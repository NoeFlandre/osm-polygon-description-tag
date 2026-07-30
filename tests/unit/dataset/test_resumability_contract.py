"""Comprehensive resumability tests: per-PBF state machine + identity contracts.

This module proves that the public orchestrator distinguishes three mutually
exclusive per-source outcomes:

- ``built-needs-upload`` — a fresh parquet was produced and must be uploaded.
- ``reused-local-needs-upload`` — a valid local artifact exists but has no
  matching remote state, so it must be uploaded.
- ``already-published`` — the local artifact matches the publication state, so
  nothing must happen on disk and nothing must be uploaded.

Only the first two outcomes may invoke the upload runner. A second unchanged
run must perform zero uploads, zero parquet writes, and zero manifest writes.
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pytest
from shapely import to_wkb
from shapely.geometry import Polygon

from osm_polygon_description_tag.config import Paths
from osm_polygon_description_tag.dataset.manifest import (
    Manifest,
    RunCounts,
    current_area_policy_sha256,
    current_code_revision,
    current_output_algorithm_revision,
    output_identity_for,
    source_identity_for,
    write_manifest,
)
from osm_polygon_description_tag.dataset.storage import write_geoparquet
from osm_polygon_description_tag.discovery import Source
from osm_polygon_description_tag.extraction import ExportRecord
from osm_polygon_description_tag.orchestrator import (
    PUBLICATION_STATE_FILENAME,
    OrchestratorError,
    _process_one,
    run_and_publish,
)
from tests.conftest import make_record_dict

_CLOCK = "2026-07-27T00:00:00+00:00"


def _frozen_clock() -> str:
    return _CLOCK


def _setup_workspace(tmp_path: Path) -> tuple[Paths, Path, Path]:
    source_root = tmp_path / "raw"
    data_root = tmp_path / "generated"
    source_root.mkdir()
    data_root.mkdir()
    (source_root / "a.osm.pbf").write_bytes(b"a-bytes")
    return Paths(source_root=source_root, data_root=data_root), source_root, data_root


def _fake_exporter() -> object:
    def _export(source_path: Path, _cfg: Path) -> object:
        stem = source_path.name.removesuffix(".osm.pbf")
        osm_id = abs(hash(stem)) % 1000000
        geom = Polygon([(0, 0), (0, 1), (1, 1), (1, 0)])
        ewkb = to_wkb(geom, include_srid=True, flavor="extended", byte_order=1)
        record = ExportRecord(
            geometry_ewkb_hex=ewkb.hex(),
            osm_type="way",
            osm_id=osm_id,
            version=1,
            changeset=1,
            timestamp="2026-01-01T00:00:00Z",
            tags=json.loads('{"description": "x"}'),
        )
        return iter([record])

    return _export


def _state_byte_count(data_root: Path) -> int:
    state_path = data_root / PUBLICATION_STATE_FILENAME
    return state_path.stat().st_size if state_path.is_file() else 0


def _state_sha(data_root: Path) -> str:
    state_path = data_root / PUBLICATION_STATE_FILENAME
    if not state_path.is_file():
        return ""
    return hashlib.sha256(state_path.read_bytes()).hexdigest()


# ---------------------------------------------------------------------------
# Outcome state-machine tests
# ---------------------------------------------------------------------------


def test_process_one_returns_built_needs_upload_when_fresh(tmp_path: Path) -> None:
    paths, source_root, data_root = _setup_workspace(tmp_path)
    source = Source(
        path=source_root / "a.osm.pbf",
        name="a.osm.pbf",
        output_name="a.parquet",
        size_bytes=0,
        mtime_ns=0,
    )
    outcome = _process_one(
        source,
        paths,
        clock=_frozen_clock,
        exporter=_fake_exporter(),
    )
    assert outcome.status == "built-needs-upload"
    assert outcome.remote_revision is None


def test_process_one_returns_reused_local_needs_upload_when_state_missing(
    tmp_path: Path,
) -> None:
    """Local artifact is valid but has no matching publication state."""
    paths, source_root, data_root = _setup_workspace(tmp_path)
    record = make_record_dict(
        Polygon([(0, 0), (0, 1), (1, 1), (1, 0)]),
        {"description": "x"},
        osm_id=1,
        source_pbf="a.osm.pbf",
    )
    write_geoparquet(iter([record]), data_root / "data" / "a.parquet", batch_size=10)
    write_manifest(
        Manifest(
            manifest_schema_version=2,
            schema_version=2,
            geoparquet_version="1.1.0",
            transform_algorithm_version=2,
            area_policy_sha256=current_area_policy_sha256(),
            output_algorithm_revision=current_output_algorithm_revision(),
            source=source_identity_for(source_root / "a.osm.pbf"),
            output=output_identity_for(data_root / "data" / "a.parquet"),
            osmium_version="osmium version 1.19.1",
            dependency_versions={"pyarrow": "20.0.0"},
            code_revision=current_code_revision(),
            started_at=_CLOCK,
            completed_at=_CLOCK,
            counts=RunCounts(emitted_features=1, included_rows=1, rejections={}),
        ),
        data_root / "manifests" / "a.manifest.json",
    )
    source = Source(
        path=source_root / "a.osm.pbf",
        name="a.osm.pbf",
        output_name="a.parquet",
        size_bytes=0,
        mtime_ns=0,
    )
    outcome = _process_one(
        source,
        paths,
        clock=_frozen_clock,
        exporter=_fake_exporter(),
    )
    assert outcome.status == "reused-local-needs-upload"
    assert outcome.remote_revision is None


def test_process_one_returns_already_published_when_state_matches(
    tmp_path: Path,
) -> None:
    """The local artifact matches publication state: must not be touched."""
    paths, source_root, data_root = _setup_workspace(tmp_path)
    record = make_record_dict(
        Polygon([(0, 0), (0, 1), (1, 1), (1, 0)]),
        {"description": "x"},
        osm_id=1,
        source_pbf="a.osm.pbf",
    )
    write_geoparquet(iter([record]), data_root / "data" / "a.parquet", batch_size=10)
    write_manifest(
        Manifest(
            manifest_schema_version=2,
            schema_version=2,
            geoparquet_version="1.1.0",
            transform_algorithm_version=2,
            area_policy_sha256=current_area_policy_sha256(),
            output_algorithm_revision=current_output_algorithm_revision(),
            source=source_identity_for(source_root / "a.osm.pbf"),
            output=output_identity_for(data_root / "data" / "a.parquet"),
            osmium_version="osmium version 1.19.1",
            dependency_versions={"pyarrow": "20.0.0"},
            code_revision=current_code_revision(),
            started_at=_CLOCK,
            completed_at=_CLOCK,
            counts=RunCounts(emitted_features=1, included_rows=1, rejections={}),
        ),
        data_root / "manifests" / "a.manifest.json",
    )
    state_path = data_root / PUBLICATION_STATE_FILENAME
    state_path.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "published": {
                    "a.osm.pbf": {
                        "source_sha256": source_identity_for(source_root / "a.osm.pbf").sha256,
                        "output_sha256": output_identity_for(
                            data_root / "data" / "a.parquet"
                        ).sha256,
                        "output_bytes": (data_root / "data" / "a.parquet").stat().st_size,
                        "remote_revision": "remote-published-rev",
                        "artifact_identity": "ignored",
                        "completed_at": _CLOCK,
                    }
                },
            }
        ),
        encoding="utf-8",
    )
    source = Source(
        path=source_root / "a.osm.pbf",
        name="a.osm.pbf",
        output_name="a.parquet",
        size_bytes=0,
        mtime_ns=0,
    )
    outcome = _process_one(
        source,
        paths,
        clock=_frozen_clock,
        exporter=_fake_exporter(),
    )
    assert outcome.status == "already-published"
    assert outcome.remote_revision is None


# ---------------------------------------------------------------------------
# Two-run behavior: no duplicate uploads
# ---------------------------------------------------------------------------


def test_run_and_publish_second_run_does_not_upload_already_published(
    tmp_path: Path,
) -> None:
    paths, source_root, data_root = _setup_workspace(tmp_path)
    uploaded: list[list[str]] = []

    def upload_runner(command: list[str]) -> str:
        uploaded.append(command)
        return "rev-1"

    report = run_and_publish(
        paths=paths,
        confirm_repo="NoeFlandre/osm-polygon-description-tag",
        preflight=lambda: {"preflight": "stub", "source_count": 1},
        upload_runner=upload_runner,
        clock=_frozen_clock,
        exporter=_fake_exporter(),
        verifier=lambda repo_id, files: "verified-rev-1",
    )
    first_upload_count = len(uploaded)
    # First run: per-PBF upload + final metadata upload.
    assert first_upload_count == 2
    assert report.outcomes[0].status in {"built-needs-upload", "reused-local-needs-upload"}
    assert report.outcomes[0].remote_revision == "verified-rev-1"

    parquet_before = (data_root / "data" / "a.parquet").read_bytes()
    manifest_before = (data_root / "manifests" / "a.manifest.json").read_bytes()
    state_before = _state_sha(data_root)
    first_snapshot = list(uploaded)

    # Second run: publisher must not be called at all.
    report = run_and_publish(
        paths=paths,
        confirm_repo="NoeFlandre/osm-polygon-description-tag",
        preflight=lambda: {"preflight": "stub", "source_count": 1},
        upload_runner=upload_runner,
        clock=_frozen_clock,
        exporter=_fake_exporter(),
        verifier=lambda repo_id, files: "verified-rev-1",
    )
    assert uploaded == first_snapshot  # no new command recorded
    assert report.outcomes[0].status == "already-published"
    # Byte-for-byte identity preserved across runs.
    assert (data_root / "data" / "a.parquet").read_bytes() == parquet_before
    assert (data_root / "manifests" / "a.manifest.json").read_bytes() == manifest_before
    assert _state_sha(data_root) == state_before


def test_run_and_publish_complete_check_rejects_extra_artifact(tmp_path: Path) -> None:
    """A stray parquet+manifest with no source must fail the final check."""
    paths, source_root, data_root = _setup_workspace(tmp_path)
    record = make_record_dict(
        Polygon([(0, 0), (0, 1), (1, 1), (1, 0)]),
        {"description": "x"},
        osm_id=999,
        source_pbf="stray.osm.pbf",
    )
    write_geoparquet(iter([record]), data_root / "data" / "stray.parquet", batch_size=10)
    write_manifest(
        Manifest(
            manifest_schema_version=2,
            schema_version=2,
            geoparquet_version="1.1.0",
            transform_algorithm_version=2,
            output_algorithm_revision="x" * 64,
            area_policy_sha256="x" * 64,
            source=source_identity_for(source_root / "a.osm.pbf"),
            output=output_identity_for(data_root / "data" / "stray.parquet"),
            osmium_version="osmium version 1.19.1",
            dependency_versions={"pyarrow": "20.0.0"},
            code_revision=current_code_revision(),
            started_at=_CLOCK,
            completed_at=_CLOCK,
            counts=RunCounts(emitted_features=1, included_rows=1, rejections={}),
        ),
        data_root / "manifests" / "stray.manifest.json",
    )
    with pytest.raises(Exception, match="completeness"):
        run_and_publish(
            paths=paths,
            confirm_repo="NoeFlandre/osm-polygon-description-tag",
            preflight=lambda: {"preflight": "stub", "source_count": 1},
            upload_runner=lambda command: "r",
            clock=_frozen_clock,
            exporter=_fake_exporter(),
            verifier=lambda repo_id, files: "rev",
        )


def test_run_and_publish_complete_check_rejects_missing_manifest(tmp_path: Path) -> None:
    """A parquet without matching manifest must fail the final check."""
    paths, source_root, data_root = _setup_workspace(tmp_path)
    record = make_record_dict(
        Polygon([(0, 0), (0, 1), (1, 1), (1, 0)]),
        {"description": "x"},
        osm_id=999,
        source_pbf="a.osm.pbf",
    )
    write_geoparquet(iter([record]), data_root / "data" / "a.parquet", batch_size=10)
    # Bypass the orchestrator's pre-build check by directly invoking completeness
    # on the inconsistent state.
    from osm_polygon_description_tag.discovery import discover_sources
    from osm_polygon_description_tag.orchestrator import _verify_final_completeness

    sources = discover_sources(paths.source_root)
    with pytest.raises(Exception, match="completeness"):
        _verify_final_completeness(paths, sources)


def test_run_and_publish_local_artifact_uploaded_without_rebuild(tmp_path: Path) -> None:
    """A valid local parquet with no state must be uploaded, not rebuilt."""
    paths, source_root, data_root = _setup_workspace(tmp_path)
    record = make_record_dict(
        Polygon([(0, 0), (0, 1), (1, 1), (1, 0)]),
        {"description": "x"},
        osm_id=1,
        source_pbf="a.osm.pbf",
    )
    write_geoparquet(iter([record]), data_root / "data" / "a.parquet", batch_size=10)
    write_manifest(
        Manifest(
            manifest_schema_version=2,
            schema_version=2,
            geoparquet_version="1.1.0",
            transform_algorithm_version=2,
            area_policy_sha256=current_area_policy_sha256(),
            output_algorithm_revision=current_output_algorithm_revision(),
            source=source_identity_for(source_root / "a.osm.pbf"),
            output=output_identity_for(data_root / "data" / "a.parquet"),
            osmium_version="osmium version 1.19.1",
            dependency_versions={"pyarrow": "20.0.0"},
            code_revision=current_code_revision(),
            started_at=_CLOCK,
            completed_at=_CLOCK,
            counts=RunCounts(emitted_features=1, included_rows=1, rejections={}),
        ),
        data_root / "manifests" / "a.manifest.json",
    )
    upload_calls = {"count": 0}
    build_calls = {"count": 0}

    original_exporter = _fake_exporter()

    def counting_exporter(source_path: Path, cfg: Path) -> object:
        build_calls["count"] += 1
        return original_exporter(source_path, cfg)

    def upload_runner(command: list[str]) -> str:
        upload_calls["count"] += 1
        return "rev-1"

    report = run_and_publish(
        paths=paths,
        confirm_repo="NoeFlandre/osm-polygon-description-tag",
        preflight=lambda: {"preflight": "stub", "source_count": 1},
        upload_runner=upload_runner,
        clock=_frozen_clock,
        exporter=counting_exporter,
        verifier=lambda repo_id, files: "verified-rev-1",
    )
    assert upload_calls["count"] == 2  # per-PBF + final metadata
    assert build_calls["count"] == 0  # local artifact reused, no fresh build
    assert report.outcomes[0].status == "reused-local-needs-upload"
    assert report.outcomes[0].remote_revision == "verified-rev-1"


def test_run_and_publish_state_written_only_after_remote_verify(tmp_path: Path) -> None:
    """An upload error must not produce publication state."""
    paths, source_root, data_root = _setup_workspace(tmp_path)
    import subprocess

    def failing_upload(command: list[str]) -> str:
        raise subprocess.CalledProcessError(1, command)

    with pytest.raises(OrchestratorError):
        run_and_publish(
            paths=paths,
            confirm_repo="NoeFlandre/osm-polygon-description-tag",
            preflight=lambda: {"preflight": "stub", "source_count": 1},
            upload_runner=failing_upload,
            clock=_frozen_clock,
            exporter=_fake_exporter(),
            verifier=lambda repo_id, files: "rev",
        )
    state_path = data_root / PUBLICATION_STATE_FILENAME
    assert not state_path.is_file()


def test_run_and_publish_publication_plan_validated_immediately_before_upload(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The per-PBF UploadPlan is revalidated via create_upload_plan right before upload."""
    paths, source_root, data_root = _setup_workspace(tmp_path)
    validation_calls: list[Path] = []
    import osm_polygon_description_tag.orchestrator as orch

    original_create = orch.create_upload_plan

    def counting_create(root: Path):
        validation_calls.append(root)
        return original_create(root)

    monkeypatch.setattr(orch, "create_upload_plan", counting_create)

    run_and_publish(
        paths=paths,
        confirm_repo="NoeFlandre/osm-polygon-description-tag",
        preflight=lambda: {"preflight": "stub", "source_count": 1},
        upload_runner=lambda command: "rev",
        clock=_frozen_clock,
        exporter=_fake_exporter(),
        verifier=lambda repo_id, files: "rev",
    )
    assert validation_calls
    assert validation_calls[0].resolve(strict=False) == data_root.resolve(strict=False)
