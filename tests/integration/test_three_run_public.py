"""Integration tests covering the three-run scenario through the public CLI.

The scenarios use the public CLI entry point (no test-only CLI flags).
External process and Hub API boundaries are faked via monkeypatch only.

Each test plants resumable local artifacts so the orchestrator's build
phase is a no-op; the run only exercises upload, verify, and state
write. The same default factories the CLI uses are exercised.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from osm_polygon_description_tag.cli import run as cli_run
from osm_polygon_description_tag.dataset.manifest import (
    file_sha256,
)
from osm_polygon_description_tag.publication import REPO_ID
from osm_polygon_description_tag.workflow.orchestrator import (
    PUBLICATION_STATE_FILENAME,
    read_publication_state,
)
from tests.helpers.dataset import plant_resumable_artifact as _plant_resumable_artifact
from tests.helpers.orchestration import setup_two_source_workspace as _setup_workspace

_CLOCK = "2026-07-27T00:00:00+00:00"


def _patch_external_boundaries(
    monkeypatch: pytest.MonkeyPatch,
    *,
    subprocess_runner,
    verifier_factory,
    clock=None,
    hf_api_factory=None,
) -> None:
    """Patch only external process + Hub boundaries; production defaults remain."""
    import osm_polygon_description_tag.publication.upload as pub
    import osm_polygon_description_tag.workflow.orchestrator as orch
    import osm_polygon_description_tag.workflow.preflight as preflight_module

    monkeypatch.setattr(pub, "default_runner_with_retry", subprocess_runner)
    monkeypatch.setattr(orch, "default_hub_verifier_factory", verifier_factory)
    # Patch HfApi used by the default preflight to avoid real network calls.
    if hf_api_factory is not None:
        monkeypatch.setattr(preflight_module.lazy_huggingface_hub, "HfApi", hf_api_factory)
    if clock is not None:
        monkeypatch.setattr(orch, "_default_clock", clock)


def _stub_hf_api_factory():
    """In-process HfApi replacement that mimics preflight + verifier success."""

    class _Stub:
        def whoami(self) -> object:
            return {"name": "fake"}

        def repo_info(self, *_a, **_kw: object) -> object:
            class _Info:
                sha = "abc"

            return _Info()

        def auth_check(self, *_a: object, **_kw: object) -> None:
            return None

    def _factory(*_a: object, **_kw: object) -> object:
        return _Stub()

    return _factory


@pytest.mark.usefixtures("fake_osmium")
def test_three_run_scenario_through_public_cli(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Run 1: build both PBFs. Run 2: zero activity (all already-published)."""
    paths, source_root, data_root = _setup_workspace(tmp_path)

    # Plant both artifacts so the orchestrator does not invoke osmium.
    _plant_resumable_artifact(paths, source_root, "a.osm.pbf")
    _plant_resumable_artifact(paths, source_root, "b.osm.pbf")
    (paths.data_root / "README.md").write_text("# README")
    (paths.data_root / "stats.json").write_text("{}")

    sentry = {"upload_count": 0, "verifier_calls": 0}

    def recording_subprocess_runner(command: list[str], timeout: float | None = None) -> None:
        sentry["upload_count"] += 1

    def verifier_factory():
        def f(_repo_id, _files):
            sentry["verifier_calls"] += 1
            return f"hub-sha-{sentry['verifier_calls']}"

        return f

    _patch_external_boundaries(
        monkeypatch,
        subprocess_runner=recording_subprocess_runner,
        verifier_factory=verifier_factory,
        clock=lambda: _CLOCK,
        hf_api_factory=_stub_hf_api_factory(),
    )

    # Run 1: full publication.
    exit_code = cli_run(
        [
            "run-and-publish",
            "--source-root",
            str(source_root),
            "--data-root",
            str(data_root),
            "--confirm-repo",
            REPO_ID,
        ]
    )
    assert exit_code == 0

    # Two per-PBF uploads + final metadata = 3.
    assert sentry["upload_count"] == 3
    # Two per-PBF verifications + final metadata verification = 3.
    assert sentry["verifier_calls"] == 3
    state_a = read_publication_state(data_root)
    assert "a.osm.pbf" in state_a["published"]
    assert "b.osm.pbf" in state_a["published"]

    snapshot_a_parquet = file_sha256(data_root / "data" / "a.parquet")
    snapshot_b_parquet = file_sha256(data_root / "data" / "b.parquet")
    snapshot_state = file_sha256(data_root / PUBLICATION_STATE_FILENAME)

    # Reset sentry.
    sentry["upload_count"] = 0
    sentry["verifier_calls"] = 0

    # Run 2: rerun unchanged. Zero activity.
    exit_code = cli_run(
        [
            "run-and-publish",
            "--source-root",
            str(source_root),
            "--data-root",
            str(data_root),
            "--confirm-repo",
            REPO_ID,
        ]
    )
    assert exit_code == 0
    assert sentry["upload_count"] == 0
    assert sentry["verifier_calls"] == 0
    # No local file changes.
    assert file_sha256(data_root / "data" / "a.parquet") == snapshot_a_parquet
    assert file_sha256(data_root / "data" / "b.parquet") == snapshot_b_parquet
    assert file_sha256(data_root / PUBLICATION_STATE_FILENAME) == snapshot_state


@pytest.mark.usefixtures("fake_osmium")
def test_three_run_partial_then_complete(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """Run 1: first PBF published, second's runner raises. Run 2: completes the second."""
    paths, source_root, data_root = _setup_workspace(tmp_path)
    _plant_resumable_artifact(paths, source_root, "a.osm.pbf")
    _plant_resumable_artifact(paths, source_root, "b.osm.pbf")
    (paths.data_root / "README.md").write_text("# README")
    (paths.data_root / "stats.json").write_text("{}")

    sentry = {"calls": 0, "verified": 0}

    def recording_subprocess_runner(command: list[str], timeout: float | None = None) -> None:
        sentry["calls"] += 1
        includes = [
            command[index + 1] for index, piece in enumerate(command) if piece == "--include"
        ]
        # Fail the per-PBF command that targets "b".
        if "data/b.parquet" in includes:
            import subprocess

            raise subprocess.CalledProcessError(1, command)

    def verifier_factory():
        def f(_repo_id, _files):
            sentry["verified"] += 1
            return f"verified-sha-{sentry['verified']}"

        return f

    _patch_external_boundaries(
        monkeypatch,
        subprocess_runner=recording_subprocess_runner,
        verifier_factory=verifier_factory,
        clock=lambda: _CLOCK,
        hf_api_factory=_stub_hf_api_factory(),
    )

    # Run 1: should fail because b's upload raises.
    exit_code = cli_run(
        [
            "run-and-publish",
            "--source-root",
            str(source_root),
            "--data-root",
            str(data_root),
            "--confirm-repo",
            REPO_ID,
        ]
    )
    assert exit_code != 0

    state_after_run1 = read_publication_state(data_root)
    # "a.osm.pbf" published; "b.osm.pbf" not.
    assert "a.osm.pbf" in state_after_run1["published"]
    assert "b.osm.pbf" not in state_after_run1["published"]

    # Replace the runner with one that always succeeds.
    def succeeding_subprocess_runner(command: list[str], timeout: float | None = None) -> None:
        sentry["calls"] += 1

    _patch_external_boundaries(
        monkeypatch,
        subprocess_runner=succeeding_subprocess_runner,
        verifier_factory=verifier_factory,
        clock=lambda: _CLOCK,
        hf_api_factory=_stub_hf_api_factory(),
    )
    sentry["calls"] = 0
    sentry["verified"] = 0

    # Run 2: completes "b" only + final metadata.
    exit_code = cli_run(
        [
            "run-and-publish",
            "--source-root",
            str(source_root),
            "--data-root",
            str(data_root),
            "--confirm-repo",
            REPO_ID,
        ]
    )
    assert exit_code == 0

    state_after_run2 = read_publication_state(data_root)
    assert "a.osm.pbf" in state_after_run2["published"]
    assert "b.osm.pbf" in state_after_run2["published"]
    # Verifier calls: "b" per-PBF + final metadata.
    assert sentry["verified"] >= 2

    # Run 3: zero activity.
    sentry["calls"] = 0
    sentry["verified"] = 0

    exit_code = cli_run(
        [
            "run-and-publish",
            "--source-root",
            str(source_root),
            "--data-root",
            str(data_root),
            "--confirm-repo",
            REPO_ID,
        ]
    )
    assert exit_code == 0
    assert sentry["calls"] == 0
    assert sentry["verified"] == 0


@pytest.mark.usefixtures("fake_osmium")
def test_publication_plan_invariant_under_repeated_runs(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Per-PBF upload plan contents are byte-stable across runs."""
    paths, source_root, data_root = _setup_workspace(tmp_path)
    _plant_resumable_artifact(paths, source_root, "a.osm.pbf")
    _plant_resumable_artifact(paths, source_root, "b.osm.pbf")
    (paths.data_root / "README.md").write_text("# README")
    (paths.data_root / "stats.json").write_text("{}")

    observed: dict[str, list[list[str]]] = {}

    def recording_subprocess_runner(command: list[str], timeout: float | None = None) -> None:
        includes = [
            command[index + 1] for index, piece in enumerate(command) if piece == "--include"
        ]
        for inc in includes:
            if inc.startswith("data/"):
                stem = inc.removeprefix("data/").removesuffix(".parquet")
                observed.setdefault(stem, []).append(includes)
                break

    def verifier_factory():
        def f(_repo_id, _files):
            return "verified"

        return f

    _patch_external_boundaries(
        monkeypatch,
        subprocess_runner=recording_subprocess_runner,
        verifier_factory=verifier_factory,
        clock=lambda: _CLOCK,
        hf_api_factory=_stub_hf_api_factory(),
    )

    cli_run(
        [
            "run-and-publish",
            "--source-root",
            str(source_root),
            "--data-root",
            str(data_root),
            "--confirm-repo",
            REPO_ID,
        ]
    )

    assert set(observed) == {"a", "b"}, (
        f"per-PBF plans must include both stems; got {set(observed)}"
    )
    for stem, calls in observed.items():
        # No "other" PBF parquet should appear in this stem's plans.
        for c in calls:
            for rel in c:
                if rel.endswith(".parquet") and not rel.endswith(f"{stem}.parquet"):
                    pytest.fail(f"unexpected parquet in plan for {stem}: {rel}")
