"""Safety contracts of the multi-shard Grid'5000 driver.

The driver may only sequence the CLI; it must never decide to spend resources.
These tests pin the three properties that make that true: it halts instead of
resubmitting anything ambiguous, it never enables daytime submission on its
own, and it processes the snapshot's shards in the snapshot's own order.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from scripts.run_language_grid import (
    DriverError,
    Remote,
    _parse_args,
    _shard_slug,
    shards_of,
    submit,
)


def _remote() -> Remote:
    return Remote(
        "nancy",
        "/home/op/bundles",
        "/home/op/models/glotlid-v3/model_v3.bin",
        "/home/op/models/sat-3l-sm/model.safetensors",
    )


def _args(**overrides: Any) -> Any:
    argv = [
        "--run-dir",
        "/run",
        "--source-root",
        "/source",
        "--retrieval-dir",
        "/retrieve",
        "--remote-bundle-root",
        "/home/op/bundles",
        "--remote-glotlid-model-path",
        "/home/op/models/glotlid-v3/model_v3.bin",
        "--remote-sat-model-path",
        "/home/op/models/sat-3l-sm/model.safetensors",
        "--remote-operator-dir",
        "/home/op/project",
        "--remote-cli",
        "/home/op/.venv/bin/osm-polygon-description-tag",
        *overrides.pop("extra", []),
    ]
    args = _parse_args(argv)
    for key, value in overrides.items():
        setattr(args, key, value)
    return args


def _plan() -> dict[str, str]:
    return {
        "remote_project_dir": "/home/op/bundles/x/project",
        "remote_source_dir": "/home/op/bundles/x/source",
        "remote_run_dir": "/home/op/bundles/x/run",
    }


@pytest.mark.parametrize("outcome", ["ambiguous", "rejected", "", "SUBMITTED_MAYBE"])
def test_a_submission_that_is_not_cleanly_submitted_halts_the_run(
    outcome: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    """An ambiguous submission means a job may exist; the driver must stop."""
    monkeypatch.setattr(
        "scripts.run_language_grid._ssh",
        lambda *args, **kwargs: {"outcome": outcome, "job_id": None},
    )

    with pytest.raises(DriverError) as caught:
        submit(_args(), _remote(), "region.parquet", _plan())

    assert "do not resubmit" in str(caught.value)


def test_daytime_submission_is_off_unless_the_operator_asks_for_it(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    sent: list[str] = []

    def fake_ssh(remote: Remote, command: str, *, capture_json: bool = False) -> dict[str, Any]:
        sent.append(command)
        return {"outcome": "submitted", "job_id": 1}

    monkeypatch.setattr("scripts.run_language_grid._ssh", fake_ssh)

    submit(_args(), _remote(), "region.parquet", _plan())

    assert "--allow-daytime" not in sent[0]
    assert "--apply" in sent[0]


def test_daytime_submission_is_forwarded_only_when_explicitly_requested(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    sent: list[str] = []

    def fake_ssh(remote: Remote, command: str, *, capture_json: bool = False) -> dict[str, Any]:
        sent.append(command)
        return {"outcome": "submitted", "job_id": 1}

    monkeypatch.setattr("scripts.run_language_grid._ssh", fake_ssh)

    submit(_args(extra=["--allow-daytime"]), _remote(), "region.parquet", _plan())

    assert "--allow-daytime" in sent[0]


def test_the_driver_defaults_to_one_core_sized_bounded_jobs() -> None:
    args = _args()

    assert args.walltime_seconds == 1800
    assert args.processing_seconds == 1200
    assert args.processing_seconds < args.walltime_seconds
    assert args.batch_size == 512
    assert args.allow_daytime is False


def test_shards_are_read_in_snapshot_order_with_their_row_counts(tmp_path: Path) -> None:
    (tmp_path / "snapshot.json").write_text(
        json.dumps(
            {
                "source_files": [
                    {"relative_path": "b.parquet", "row_count": 2},
                    {"relative_path": "a.parquet", "row_count": 1},
                ]
            }
        ),
        encoding="utf-8",
    )

    assert shards_of(tmp_path) == (("b.parquet", 2), ("a.parquet", 1))


@pytest.mark.parametrize(
    ("shard", "slug"),
    [
        ("region.parquet", "region-parquet"),
        ("a/b c.parquet", "a-b-c-parquet"),
        ("$(evil).parquet", "evil--parquet"),
    ],
)
def test_a_remote_directory_name_cannot_carry_shell_metacharacters(shard: str, slug: str) -> None:
    """OAR evaluates stored commands through a shell, so the name must be inert."""
    result = _shard_slug(shard)

    assert result == slug
    assert all(character.isalnum() or character == "-" for character in result)


def test_the_driver_stages_and_forwards_the_pinned_sat_model_path() -> None:
    """Splitting runs in the same job, so the driver must name the SaT weights."""
    remote = _remote()

    assert remote.sat_model_path == "/home/op/models/sat-3l-sm/model.safetensors"


def test_every_remote_model_path_is_required_by_the_driver() -> None:
    """A run that forgets either artifact must fail on the frontend, not the node."""
    args = _args()

    assert args.remote_sat_model_path == "/home/op/models/sat-3l-sm/model.safetensors"
    assert args.remote_glotlid_model_path == "/home/op/models/glotlid-v3/model_v3.bin"
