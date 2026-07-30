"""RED tests proving final metadata publication is independently resumable.

Current behavior: each PBF is marked published before the final README/stats
upload. If the final metadata upload or verification fails, every PBF is
already-published; the next invocation exits when per_pbf_upload_count == 0
and never retries the metadata upload.

Amended behavior:

- publication-state.json records a dataset-level metadata state with at
  minimum: exact metadata UploadPlan identity, README.md SHA-256 + size,
  stats.json SHA-256 + size, verified remote revision, completion timestamp.
- After processing sources, the orchestrator constructs the current
  two-file metadata plan and compares its identity against the verified
  metadata state.
- If the identity is absent or differs, the orchestrator uploads and
  verifies the metadata even when zero PBFs were built/uploaded during
  this invocation.
- The metadata state is written atomically only after remote verification.
- If upload succeeds but the process stops before state is written, a
  retry must be safe (idempotent upload).
- A failed metadata upload or verification returns non-zero AND leaves
  metadata state incomplete.
- An unchanged fully completed third run performs zero builds, zero
  uploads, zero verifier calls, and zero local rewrites.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from shapely.geometry import Polygon

from osm_polygon_description_tag.cli import run as cli_run
from osm_polygon_description_tag.config import Paths
from osm_polygon_description_tag.manifest import (
    Manifest,
    RunCounts,
    current_area_policy_sha256,
    current_output_algorithm_revision,
    file_sha256,
    output_identity_for,
    source_identity_for,
    write_manifest,
)
from osm_polygon_description_tag.orchestrator import (
    PUBLICATION_STATE_FILENAME,
    read_publication_state,
)
from osm_polygon_description_tag.publication import (
    REPO_ID,
    _build_metadata_only_upload_plan,
)
from osm_polygon_description_tag.storage import write_geoparquet
from tests.conftest import make_record_dict

_CLOCK = "2026-07-27T00:00:00+00:00"


def _setup_workspace(tmp_path: Path) -> tuple[Paths, Path, Path]:
    source_root = tmp_path / "raw"
    data_root = tmp_path / "generated"
    source_root.mkdir()
    data_root.mkdir()
    (source_root / "a.osm.pbf").write_bytes(b"a-bytes")
    (source_root / "b.osm.pbf").write_bytes(b"b-bytes")
    return Paths(source_root=source_root, data_root=data_root), source_root, data_root


def _plant_resumable_artifact(data_root: Path, source_root: Path, source_name: str) -> None:
    stem = source_name.removesuffix(".osm.pbf")
    (data_root / "data").mkdir(parents=True, exist_ok=True)
    (data_root / "manifests").mkdir(parents=True, exist_ok=True)
    write_geoparquet(
        iter(
            [
                make_record_dict(
                    Polygon([(0, 0), (0, 1), (1, 1), (1, 0)]),
                    {"description": "x"},
                    osm_id=1,
                    source_pbf=source_name,
                )
            ]
        ),
        data_root / "data" / f"{stem}.parquet",
        batch_size=10,
    )
    source_path = source_root / source_name
    write_manifest(
        Manifest(
            manifest_schema_version=2,
            schema_version=2,
            geoparquet_version="1.1.0",
            transform_algorithm_version=2,
            area_policy_sha256=current_area_policy_sha256(),
            output_algorithm_revision=current_output_algorithm_revision(),
            source=source_identity_for(source_path),
            output=output_identity_for(data_root / "data" / f"{stem}.parquet"),
            osmium_version="osmium version 1.19.1",
            dependency_versions={"pyarrow": "20.0.0"},
            code_revision=None,
            started_at="2026-07-27T00:00:00+00:00",
            completed_at="2026-07-27T00:01:00+00:00",
            counts=RunCounts(emitted_features=1, included_rows=1, rejections={}),
        ),
        data_root / "manifests" / f"{stem}.manifest.json",
    )


def _plant_metadata(data_root: Path) -> None:
    (data_root / "README.md").write_text("# README")
    (data_root / "stats.json").write_text("{}")


def _patch_external_boundaries(
    monkeypatch: pytest.MonkeyPatch,
    *,
    subprocess_runner,
    verifier_factory,
    metadata_runner=None,
):
    """Patch external boundaries only; production defaults remain.

    ``metadata_runner`` overrides the subprocess runner for the FINAL
    metadata upload (the runner after all per-PBF uploads). It is used
    to simulate metadata failures without failing per-PBF uploads.
    """
    import osm_polygon_description_tag.publication.upload as pub
    import osm_polygon_description_tag.workflow.orchestrator as orch
    import osm_polygon_description_tag.workflow.preflight as preflight_module

    call_log: dict[str, int] = {"uploads": 0, "verifier_calls": 0}

    def counting_runner(command: list[str], timeout: float | None = None) -> None:
        includes = [
            command[index + 1] for index, piece in enumerate(command) if piece == "--include"
        ]
        if metadata_runner is not None and not any(
            inc.startswith("manifests/") or inc.startswith("data/") for inc in includes
        ):
            metadata_runner(command)
            call_log["uploads"] += 1
            return
        subprocess_runner(command)
        call_log["uploads"] += 1

    def patching_verifier_factory():
        def f(_repo_id: str, _files: object) -> str:
            call_log["verifier_calls"] += 1
            return verifier_factory()(_repo_id, _files)

        return f

    # Patch HfApi to avoid live auth.
    class _Stub:
        def whoami(self) -> object:
            return {"name": "fake"}

        def repo_info(self, *_a: object, **_kw: object) -> object:
            class _Info:
                sha = "abc"

            return _Info()

        def auth_check(self, *_a: object, **_kw: object) -> None:
            return None

    monkeypatch.setattr(preflight_module._huggingface_hub, "HfApi", lambda *_a, **_kw: _Stub())
    monkeypatch.setattr(pub, "_default_runner_with_retry", counting_runner)
    monkeypatch.setattr(orch, "default_hub_verifier_factory", patching_verifier_factory)
    monkeypatch.setattr(orch, "_default_clock", lambda: _CLOCK)
    return call_log


def test_metadata_retried_after_interrupted_first_run(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Run 1 publishes PBFs but final metadata fails. Run 2 retries metadata only."""
    paths, source_root, data_root = _setup_workspace(tmp_path)
    _plant_resumable_artifact(data_root, source_root, "a.osm.pbf")
    _plant_resumable_artifact(data_root, source_root, "b.osm.pbf")
    _plant_metadata(data_root)

    def metadata_runner_fails(command: list[str]) -> None:
        import subprocess

        raise subprocess.CalledProcessError(1, command, stderr=b"upload failed")

    _patch_external_boundaries(
        monkeypatch,
        subprocess_runner=lambda command: None,
        verifier_factory=lambda: (lambda *a, **kw: "rev"),
        metadata_runner=metadata_runner_fails,
    )

    # Run 1: per-PBF uploads succeed, final metadata fails.
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

    state = read_publication_state(data_root)
    # Per-PBF states ARE recorded.
    assert "a.osm.pbf" in state["published"]
    assert "b.osm.pbf" in state["published"]
    # Metadata state is NOT recorded because upload failed.
    assert "metadata" not in state or "identity_sha256" not in state.get("metadata", {})

    # Run 2: no per-PBF uploads, retry metadata only.
    log2 = _patch_external_boundaries(
        monkeypatch,
        subprocess_runner=lambda command: None,
        verifier_factory=lambda: (lambda *a, **kw: "rev-meta"),
        metadata_runner=None,  # metadata succeeds
    )

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

    # Run 2 performed exactly one upload (the metadata) and one verifier
    # call (the metadata).
    assert log2["uploads"] == 1
    assert log2["verifier_calls"] >= 1

    state = read_publication_state(data_root)
    assert "metadata" in state
    assert "identity_sha256" in state["metadata"]
    assert "verified_revision" in state["metadata"]
    assert "completed_at" in state["metadata"]
    assert "readme_sha256" in state["metadata"]
    assert "stats_sha256" in state["metadata"]
    assert "readme_size_bytes" in state["metadata"]
    assert "stats_size_bytes" in state["metadata"]

    # Run 3: zero activity.
    log3 = _patch_external_boundaries(
        monkeypatch,
        subprocess_runner=lambda command: None,
        verifier_factory=lambda: (lambda *a, **kw: "rev"),
    )
    snapshot_state = file_sha256(data_root / PUBLICATION_STATE_FILENAME)
    snapshot_readme = file_sha256(data_root / "README.md")
    snapshot_stats = file_sha256(data_root / "stats.json")

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
    assert log3["uploads"] == 0
    assert log3["verifier_calls"] == 0
    assert file_sha256(data_root / PUBLICATION_STATE_FILENAME) == snapshot_state
    assert file_sha256(data_root / "README.md") == snapshot_readme
    assert file_sha256(data_root / "stats.json") == snapshot_stats


def test_metadata_state_records_required_fields(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """The recorded metadata state includes identity, SHAs, sizes, revision, timestamp."""
    paths, source_root, data_root = _setup_workspace(tmp_path)
    (source_root / "b.osm.pbf").unlink()
    _plant_resumable_artifact(data_root, source_root, "a.osm.pbf")
    _plant_metadata(data_root)

    _patch_external_boundaries(
        monkeypatch,
        subprocess_runner=lambda command: None,
        verifier_factory=lambda: (lambda *a, **kw: "rev-meta-1"),
    )

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

    state = read_publication_state(data_root)
    metadata = state["metadata"]
    expected_plan = _build_metadata_only_upload_plan(data_root)
    assert metadata["identity_sha256"] == expected_plan.identity_sha256
    assert metadata["readme_sha256"] == file_sha256(data_root / "README.md")
    assert metadata["stats_sha256"] == file_sha256(data_root / "stats.json")
    assert metadata["readme_size_bytes"] == (data_root / "README.md").stat().st_size
    assert metadata["stats_size_bytes"] == (data_root / "stats.json").stat().st_size
    assert metadata["verified_revision"] == "rev-meta-1"
    assert metadata["completed_at"] == _CLOCK


def test_metadata_upload_failure_leaves_state_incomplete(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """A failed metadata upload leaves publication_state.json's metadata incomplete."""
    paths, source_root, data_root = _setup_workspace(tmp_path)
    (source_root / "b.osm.pbf").unlink()
    _plant_resumable_artifact(data_root, source_root, "a.osm.pbf")
    _plant_metadata(data_root)

    def metadata_runner_fails(command: list[str]) -> None:
        import subprocess

        raise subprocess.CalledProcessError(1, command, stderr=b"fail")

    _patch_external_boundaries(
        monkeypatch,
        subprocess_runner=lambda command: None,
        verifier_factory=lambda: (lambda *a, **kw: "never"),
        metadata_runner=metadata_runner_fails,
    )

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

    # Per-PBF state at least written (upload succeeded before metadata).
    state = read_publication_state(data_root)
    assert "a.osm.pbf" in state["published"]
    # Metadata state must NOT be recorded because the upload failed.
    metadata = state.get("metadata", {})
    assert "verified_revision" not in metadata


def test_metadata_verification_failure_leaves_state_incomplete(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """A failed metadata verification leaves publication_state.json incomplete."""
    paths, source_root, data_root = _setup_workspace(tmp_path)
    (source_root / "b.osm.pbf").unlink()
    _plant_resumable_artifact(data_root, source_root, "a.osm.pbf")
    _plant_metadata(data_root)

    def verifier_factory_failing():
        def f(_repo_id: str, _files: object) -> str:
            from osm_polygon_description_tag.orchestrator import HubVerificationError

            raise HubVerificationError("verifier failed")

        return f

    _patch_external_boundaries(
        monkeypatch,
        subprocess_runner=lambda command: None,
        verifier_factory=verifier_factory_failing,
    )

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

    state = read_publication_state(data_root)
    metadata = state.get("metadata", {})
    assert "verified_revision" not in metadata


def test_metadata_retry_is_idempotent(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """The metadata upload is safe to invoke multiple times (idempotent)."""
    paths, source_root, data_root = _setup_workspace(tmp_path)
    (source_root / "b.osm.pbf").unlink()
    _plant_resumable_artifact(data_root, source_root, "a.osm.pbf")
    _plant_metadata(data_root)

    call_log = {"uploads": 0}

    def counting_runner(command: list[str], timeout: float | None = None) -> None:
        call_log["uploads"] += 1

    def verifier_factory():
        def f(_repo_id: str, _files: object) -> str:
            return "rev-meta-x"

        return f

    _patch_external_boundaries(
        monkeypatch,
        subprocess_runner=counting_runner,
        verifier_factory=verifier_factory,
    )

    # Two runs in a row must succeed (the second one is a no-op for metadata
    # because the state is already verified).
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
    first_uploads = call_log["uploads"]

    # Second run: metadata state unchanged, no metadata upload.
    snapshot_state = file_sha256(data_root / PUBLICATION_STATE_FILENAME)
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
    assert call_log["uploads"] == first_uploads
    # No state rewrite.
    assert file_sha256(data_root / PUBLICATION_STATE_FILENAME) == snapshot_state


def test_metadata_uploaded_when_no_per_pbf_uploads(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Even when zero PBFs were uploaded, metadata must still be uploaded if missing."""
    paths, source_root, data_root = _setup_workspace(tmp_path)
    _plant_resumable_artifact(data_root, source_root, "a.osm.pbf")
    _plant_resumable_artifact(data_root, source_root, "b.osm.pbf")
    _plant_metadata(data_root)

    from osm_polygon_description_tag.publication import _build_per_pbf_upload_plan

    # Mark both PBFs as already-published so the run can skip per-PBF uploads.
    state_init = {
        "schema_version": 1,
        "published": {
            "a.osm.pbf": {
                "source_sha256": source_identity_for(source_root / "a.osm.pbf").sha256,
                "output_sha256": file_sha256(data_root / "data" / "a.parquet"),
                "output_bytes": (data_root / "data" / "a.parquet").stat().st_size,
                "remote_revision": "r-a",
                "artifact_identity": _build_per_pbf_upload_plan(
                    data_root, "a.osm.pbf"
                ).identity_sha256,
                "completed_at": _CLOCK,
            },
            "b.osm.pbf": {
                "source_sha256": source_identity_for(source_root / "b.osm.pbf").sha256,
                "output_sha256": file_sha256(data_root / "data" / "b.parquet"),
                "output_bytes": (data_root / "data" / "b.parquet").stat().st_size,
                "remote_revision": "r-b",
                "artifact_identity": _build_per_pbf_upload_plan(
                    data_root, "b.osm.pbf"
                ).identity_sha256,
                "completed_at": _CLOCK,
            },
        },
    }
    (data_root / PUBLICATION_STATE_FILENAME).write_text(
        json.dumps(state_init, sort_keys=True, indent=2), encoding="utf-8"
    )

    log = _patch_external_boundaries(
        monkeypatch,
        subprocess_runner=lambda command: None,
        verifier_factory=lambda: (lambda *a, **kw: "rev-meta"),
    )

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

    # Exactly one upload (the metadata) and one verifier call.
    assert log["uploads"] == 1
    assert log["verifier_calls"] >= 1
    state = read_publication_state(data_root)
    assert "metadata" in state
    assert "verified_revision" in state["metadata"]
