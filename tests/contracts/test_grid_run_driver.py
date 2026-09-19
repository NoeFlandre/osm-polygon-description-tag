"""Safety contracts of the multi-shard Grid'5000 driver.

The driver may only sequence the CLI; it must never decide to spend resources.
These tests pin the three properties that make that true: it halts instead of
resubmitting anything ambiguous, it never enables daytime submission on its
own, and it processes the snapshot's shards in the snapshot's own order.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any

import pytest

from scripts.run_language_grid import (
    _UNREADABLE_CHECKPOINT,
    DriverError,
    Remote,
    _cli,
    _parse_args,
    _shard_slug,
    awaiting_collection,
    checkpointed_shards,
    selected_shards,
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
        return _apply_payload("submitted")

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
        return _apply_payload("submitted")

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


def _apply_payload(outcome: str, *, job_id: int | None = 6923144) -> dict[str, Any]:
    """Return the payload ``language grid submit --apply`` actually prints.

    The CLI wraps the submission under ``result``; it does not put ``outcome``
    at the top level. A driver that reads the top level sees no outcome at all
    and halts a run whose job is genuinely queued, which is the worst possible
    reading: the job exists and the operator is told it does not.
    """
    return {
        "applied": True,
        "plan": {"shard": "region.parquet", "may_apply": True},
        "result": {
            "outcome": outcome,
            "job_id": job_id,
            "detail": "job submitted",
            "attempt": 1,
        },
    }


def test_a_clean_submission_in_the_cli_payload_shape_is_accepted(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A queued job must be recognised from the shape the CLI really emits."""
    monkeypatch.setattr(
        "scripts.run_language_grid._ssh", lambda *args, **kwargs: _apply_payload("submitted")
    )

    submit(_args(), _remote(), "region.parquet", _plan())


def test_the_recognised_submission_reports_the_scheduler_job_id(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """The operator needs the job id to reconcile; it must reach the log."""
    monkeypatch.setattr(
        "scripts.run_language_grid._ssh", lambda *args, **kwargs: _apply_payload("submitted")
    )

    submit(_args(), _remote(), "region.parquet", _plan())

    logged = json.loads(capsys.readouterr().out.strip().splitlines()[-1])
    assert logged["event"] == "submitted"
    assert logged["job_id"] == 6923144


@pytest.mark.parametrize("outcome", ["ambiguous", "rejected", "", "SUBMITTED_MAYBE"])
def test_an_unclean_outcome_inside_the_cli_payload_halts_the_run(
    outcome: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Reading the nested outcome must not lose the ambiguity check."""
    monkeypatch.setattr(
        "scripts.run_language_grid._ssh", lambda *args, **kwargs: _apply_payload(outcome)
    )

    with pytest.raises(DriverError) as caught:
        submit(_args(), _remote(), "region.parquet", _plan())

    assert "do not resubmit" in str(caught.value)


def test_a_refused_apply_gate_halts_the_run(monkeypatch: pytest.MonkeyPatch) -> None:
    """``result`` is null when the gate refused; nothing was submitted."""
    monkeypatch.setattr(
        "scripts.run_language_grid._ssh",
        lambda *args, **kwargs: {
            "applied": False,
            "plan": {"blocked_reason": "daytime submission is refused"},
            "result": None,
        },
    )

    with pytest.raises(DriverError) as caught:
        submit(_args(), _remote(), "region.parquet", _plan())

    assert "do not resubmit" in str(caught.value)


def test_the_whole_snapshot_is_selected_by_default() -> None:
    """Without a partition the driver must still see every shard."""
    shards = (("a.parquet", 1), ("b.parquet", 2), ("c.parquet", 3))

    assert selected_shards(shards, stride=1, index=0) == shards


@pytest.mark.parametrize("index", [0, 1, 2])
def test_a_partition_keeps_the_snapshot_order_within_its_share(index: int) -> None:
    shards = tuple((f"{n}.parquet", n) for n in range(9))

    chosen = selected_shards(shards, stride=3, index=index)

    assert chosen == tuple(shards[position] for position in range(index, 9, 3))


def test_partitions_of_one_stride_are_disjoint_and_cover_everything() -> None:
    """Two drivers must never pick the same shard, and none may be dropped."""
    shards = tuple((f"{n}.parquet", n) for n in range(20))

    parts = [selected_shards(shards, stride=4, index=index) for index in range(4)]

    flattened = [entry for part in parts for entry in part]
    assert sorted(flattened) == sorted(shards)
    assert len(flattened) == len(set(flattened))


@pytest.mark.parametrize(
    ("stride", "index"),
    [(0, 0), (-1, 0), (2, 2), (2, -1), (2, 5)],
)
def test_an_impossible_partition_is_refused(stride: int, index: int) -> None:
    """A bad partition would silently drop or double-process shards."""
    with pytest.raises(DriverError) as caught:
        selected_shards((("a.parquet", 1),), stride=stride, index=index)

    assert "partition" in str(caught.value)


def test_the_partition_defaults_to_the_whole_snapshot() -> None:
    args = _args()

    assert args.shard_stride == 1
    assert args.shard_index == 0


def test_no_queue_is_requested_unless_the_operator_names_one(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Some sites reject an explicit queue, so the bare form stays the default."""
    sent: list[str] = []

    def fake_ssh(remote: Remote, command: str, *, capture_json: bool = False) -> dict[str, Any]:
        sent.append(command)
        return _apply_payload("submitted")

    monkeypatch.setattr("scripts.run_language_grid._ssh", fake_ssh)

    submit(_args(), _remote(), "region.parquet", _plan())

    assert "--queue" not in sent[0]


def test_a_named_queue_is_forwarded_to_the_cli(monkeypatch: pytest.MonkeyPatch) -> None:
    """Four of the eight sites cannot be used at all without this."""
    sent: list[str] = []

    def fake_ssh(remote: Remote, command: str, *, capture_json: bool = False) -> dict[str, Any]:
        sent.append(command)
        return _apply_payload("submitted")

    monkeypatch.setattr("scripts.run_language_grid._ssh", fake_ssh)

    submit(_args(extra=["--queue", "default"]), _remote(), "region.parquet", _plan())

    assert "--queue default" in sent[0]


def test_the_driver_never_shells_out_through_uv() -> None:
    """Every CLI call must go straight to the interpreter's own console script.

    ``uv run`` takes a lock on the shared uv cache for the duration of the
    command. The driver invokes the CLI once per shard to test completeness, so
    routing those through ``uv run`` makes a parent holding that lock spawn a
    child that waits for it. With several drivers in parallel the run stops
    dead: seven sweep workers sat for thirty minutes with no child process at
    all. Calling the console script beside ``sys.executable`` removes the lock
    from the hot path, and a process start with it.
    """
    argv = _cli()

    assert "uv" not in argv
    assert Path(argv[0]).name == "osm-polygon-description-tag"
    assert Path(argv[0]).parent == Path(sys.executable).parent


def _write_checkpoint(run_dir: Path, slug: str, payload: object) -> Path:
    shard_dir = run_dir / "shards" / slug
    shard_dir.mkdir(parents=True)
    checkpoint = shard_dir / "checkpoint.json"
    if isinstance(payload, str):
        checkpoint.write_text(payload, encoding="utf-8")
    else:
        checkpoint.write_text(json.dumps(payload), encoding="utf-8")
    return checkpoint


def test_checkpointed_shards_is_empty_without_a_shards_directory(tmp_path: Path) -> None:
    """A run that has never written a shard cannot have a complete one."""
    assert checkpointed_shards(tmp_path) == frozenset()


def test_checkpointed_shards_reports_every_checkpointed_shard_name(tmp_path: Path) -> None:
    _write_checkpoint(tmp_path, "aaa", {"shard": "albania-latest.parquet", "status": "complete"})
    _write_checkpoint(tmp_path, "bbb", {"shard": "angola-latest.parquet", "status": "partial"})

    assert checkpointed_shards(tmp_path) == frozenset(
        {"albania-latest.parquet", "angola-latest.parquet"}
    )


def test_checkpointed_shards_reports_a_partial_checkpoint_too(tmp_path: Path) -> None:
    """The scan narrows who is asked; it must not judge completeness itself."""
    _write_checkpoint(tmp_path, "aaa", {"shard": "albania-latest.parquet", "status": "partial"})

    assert "albania-latest.parquet" in checkpointed_shards(tmp_path)


def test_an_unreadable_checkpoint_forces_the_cli_to_be_asked(tmp_path: Path) -> None:
    """Unreadable is not absent: never treat it as an unstarted shard."""
    _write_checkpoint(tmp_path, "aaa", "{not json")

    assert _UNREADABLE_CHECKPOINT in checkpointed_shards(tmp_path)


def test_a_checkpoint_without_a_shard_name_contributes_nothing(tmp_path: Path) -> None:
    _write_checkpoint(tmp_path, "aaa", {"status": "complete"})

    assert checkpointed_shards(tmp_path) == frozenset()


def _intent(**overrides: object) -> str:
    payload = {
        "outcome": "submitted",
        "result_acknowledged": False,
        "job_id": 1234,
        "shard": "angola-latest.parquet",
    }
    payload.update(overrides)
    return json.dumps(payload)


def _stub_ssh(monkeypatch: pytest.MonkeyPatch, stdout: str) -> None:
    class _Completed:
        def __init__(self) -> None:
            self.stdout = stdout
            self.returncode = 0

    monkeypatch.setattr("scripts.run_language_grid.subprocess.run", lambda *a, **k: _Completed())


def test_an_unacknowledged_submission_is_resumed_not_restaged(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Re-staging would overwrite the checkpoint the finished job committed."""
    _stub_ssh(monkeypatch, _intent())

    assert awaiting_collection(_remote(), "angola-latest.parquet") is True


def test_an_acknowledged_submission_is_not_resumed(monkeypatch: pytest.MonkeyPatch) -> None:
    _stub_ssh(monkeypatch, _intent(result_acknowledged=True))

    assert awaiting_collection(_remote(), "angola-latest.parquet") is False


def test_a_rejected_submission_is_not_resumed(monkeypatch: pytest.MonkeyPatch) -> None:
    """A rejected submission produced no results, so there is nothing to collect."""
    _stub_ssh(monkeypatch, _intent(outcome="rejected", job_id=None))

    assert awaiting_collection(_remote(), "angola-latest.parquet") is False


def test_no_recorded_submission_is_not_resumed(monkeypatch: pytest.MonkeyPatch) -> None:
    _stub_ssh(monkeypatch, "")

    assert awaiting_collection(_remote(), "angola-latest.parquet") is False


def test_several_recorded_attempts_are_never_resumed_automatically(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """More than one attempt is ambiguous; an operator must look at it."""
    _stub_ssh(monkeypatch, _intent() + "\n" + _intent(job_id=5678))

    assert awaiting_collection(_remote(), "angola-latest.parquet") is False


def test_unparseable_intent_is_not_resumed(monkeypatch: pytest.MonkeyPatch) -> None:
    _stub_ssh(monkeypatch, "{not json")

    assert awaiting_collection(_remote(), "angola-latest.parquet") is False
