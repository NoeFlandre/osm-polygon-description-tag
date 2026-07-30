"""Synthetic three-run scenario covering the full orchestrator lifecycle.

Scenario:

1. Run 1: builds and uploads two synthetic sources. Upload of ``b`` fails
   after the verified Hub commit but before the local state write.
2. Run 2: resumes, completes ``b``, leaves state consistent. Final metadata
   is also uploaded.
3. Run 3: rerun unchanged. Zero builds, zero uploads, byte-for-byte
   preservation of all artifacts and publication state.
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pytest

from osm_polygon_description_tag.config import Paths
from osm_polygon_description_tag.extraction import ExportRecord
from osm_polygon_description_tag.orchestrator import (
    PUBLICATION_STATE_FILENAME,
    run_and_publish,
)

_CLOCK = "2026-07-27T00:00:00+00:00"


def _frozen_clock() -> str:
    return _CLOCK


def _fake_exporter() -> object:
    def _export(source_path: Path, _cfg: Path) -> object:
        from shapely import to_wkb
        from shapely.geometry import Polygon

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


def _setup_workspace(tmp_path: Path) -> tuple[Paths, Path, Path]:
    source_root = tmp_path / "raw"
    data_root = tmp_path / "generated"
    source_root.mkdir()
    data_root.mkdir()
    (source_root / "a.osm.pbf").write_bytes(b"a-bytes")
    (source_root / "b.osm.pbf").write_bytes(b"b-bytes")
    return Paths(source_root=source_root, data_root=data_root), source_root, data_root


def _file_sha(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def test_three_run_scenario(tmp_path: Path) -> None:
    paths, source_root, data_root = _setup_workspace(tmp_path)

    # ---- Run 1: build both, fail to write state for ``b`` after upload.
    upload_calls: list[tuple[str, list[str]]] = []
    upload_revisions: dict[str, str] = {}

    def upload_runner(command: list[str]) -> str:
        # Identify the source by the parquet include flag.
        target = "a"
        if "data/b.parquet" in command:
            target = "b"
        upload_calls.append((target, list(command)))
        return upload_revisions.setdefault(target, f"hub-rev-{target}")

    def verifier(repo_id: str, files: tuple[object, ...]) -> str:
        # Different files count -> different "verified" string.
        if len(files) == 4:
            return "verified-per-pbf"
        return "verified-metadata"

    exporter = _fake_exporter()

    # Patch the per-PBF state writer to raise on the second source.
    from osm_polygon_description_tag.workflow import orchestrator

    real_write = orchestrator._write_publication_state
    written: list[str] = []

    def flaky_write(*args, **kwargs):
        source_name = kwargs.get("source_name", "")
        written.append(source_name)
        if source_name == "b.osm.pbf":
            raise RuntimeError("simulated crash after remote commit for b")
        return real_write(*args, **kwargs)

    monkeypatch = pytest.MonkeyPatch()
    monkeypatch.setattr(orchestrator, "_write_publication_state", flaky_write)

    with pytest.raises(Exception, match="simulated crash"):
        run_and_publish(
            paths=paths,
            confirm_repo="NoeFlandre/osm-polygon-description-tag",
            preflight=lambda: {"preflight": "stub", "source_count": 2},
            upload_runner=upload_runner,
            clock=_frozen_clock,
            exporter=exporter,
            verifier=verifier,
        )

    assert written == ["a.osm.pbf", "b.osm.pbf"]
    # Publication state recorded ``a`` but not ``b``.
    state_path = data_root / PUBLICATION_STATE_FILENAME
    assert state_path.is_file()
    state = json.loads(state_path.read_text(encoding="utf-8"))
    assert "a.osm.pbf" in state["published"]
    assert "b.osm.pbf" not in state["published"]

    # Snapshot artifacts after Run 1.
    snapshot_a = {
        "parquet": _file_sha(data_root / "data" / "a.parquet"),
        "manifest": _file_sha(data_root / "manifests" / "a.manifest.json"),
    }
    snapshot_b_parquet = _file_sha(data_root / "data" / "b.parquet")
    snapshot_state = _file_sha(state_path)
    del snapshot_state  # informational only; state is checked via load below.

    # ---- Run 2: resume, complete ``b``.
    monkeypatch.undo()

    run_and_publish(
        paths=paths,
        confirm_repo="NoeFlandre/osm-polygon-description-tag",
        preflight=lambda: {"preflight": "stub", "source_count": 2},
        upload_runner=upload_runner,
        clock=_frozen_clock,
        exporter=exporter,
        verifier=verifier,
    )

    state = json.loads(state_path.read_text(encoding="utf-8"))
    assert "a.osm.pbf" in state["published"]
    assert "b.osm.pbf" in state["published"]

    snapshot_a_parquet_after = _file_sha(data_root / "data" / "a.parquet")
    snapshot_a_manifest_after = _file_sha(data_root / "manifests" / "a.manifest.json")

    # Run 2 reused ``a``; parquet + manifest must not have changed.
    assert snapshot_a_parquet_after == snapshot_a["parquet"]
    assert snapshot_a_manifest_after == snapshot_a["manifest"]

    # ---- Run 3: rerun unchanged. Zero uploads.
    upload_calls_before = list(upload_calls)
    state_path_before = _file_sha(state_path)

    report = run_and_publish(
        paths=paths,
        confirm_repo="NoeFlandre/osm-polygon-description-tag",
        preflight=lambda: {"preflight": "stub", "source_count": 2},
        upload_runner=upload_runner,
        clock=_frozen_clock,
        exporter=exporter,
        verifier=verifier,
    )

    # No new upload commands.
    assert upload_calls == upload_calls_before
    # Both outcomes already-published.
    for outcome in report.outcomes:
        assert outcome.status == "already-published"
    # State file unchanged.
    assert _file_sha(state_path) == state_path_before
    # All artifacts byte-for-byte identical.
    assert _file_sha(data_root / "data" / "a.parquet") == snapshot_a["parquet"]
    assert _file_sha(data_root / "manifests" / "a.manifest.json") == snapshot_a["manifest"]
    assert _file_sha(data_root / "data" / "b.parquet") == snapshot_b_parquet


def test_run_and_publish_publication_plan_revalidation_before_upload(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The orchestrator calls create_upload_plan immediately before each upload."""
    paths, source_root, data_root = _setup_workspace(tmp_path)

    from osm_polygon_description_tag.workflow import orchestrator as orch

    call_count = {"value": 0}
    original_create = orch.create_upload_plan

    def counting_create(root: Path):
        call_count["value"] += 1
        return original_create(root)

    monkeypatch.setattr(orch, "create_upload_plan", counting_create)

    run_and_publish(
        paths=paths,
        confirm_repo="NoeFlandre/osm-polygon-description-tag",
        preflight=lambda: {"preflight": "stub", "source_count": 2},
        upload_runner=lambda command: "rev",
        clock=_frozen_clock,
        exporter=_fake_exporter(),
        verifier=lambda repo_id, files: "verified",
    )
    # Two per-PBF uploads + final metadata.
    assert call_count["value"] >= 3


def test_run_and_publish_completeness_uses_full_resumability(tmp_path: Path) -> None:
    """A staged-complete artifact whose output_algorithm_revision has drifted
    is rebuilt from scratch (not classified as resumable)."""
    from shapely.geometry import Polygon

    from osm_polygon_description_tag.manifest import (
        Manifest,
        current_area_policy_sha256,
        current_code_revision,
        current_output_algorithm_revision,
        output_identity_for,
        source_identity_for,
        write_manifest,
    )
    from osm_polygon_description_tag.storage import write_geoparquet
    from tests.conftest import make_record_dict

    paths, source_root, data_root = _setup_workspace(tmp_path)

    # Plant a complete, valid local artifact with a deliberately bogus
    # output_algorithm_revision; resumability must fail and the source must
    # be rebuilt. Drop ``b`` so the run works against a single source.
    (source_root / "b.osm.pbf").unlink()

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
            output_algorithm_revision="bogus-revision",
            source=source_identity_for(source_root / "a.osm.pbf"),
            output=output_identity_for(data_root / "data" / "a.parquet"),
            osmium_version="osmium version 1.19.1",
            dependency_versions={"pyarrow": "20.0.0"},
            code_revision=current_code_revision(),
            started_at="2026-07-27T00:00:00+00:00",
            completed_at="2026-07-27T00:01:00+00:00",
            counts=__import__(
                "osm_polygon_description_tag.manifest", fromlist=["RunCounts"]
            ).RunCounts(emitted_features=1, included_rows=1, rejections={}),
        ),
        data_root / "manifests" / "a.manifest.json",
    )

    run_and_publish(
        paths=paths,
        confirm_repo="NoeFlandre/osm-polygon-description-tag",
        preflight=lambda: {"preflight": "stub", "source_count": 1},
        upload_runner=lambda command: "rev",
        clock=_frozen_clock,
        exporter=_fake_exporter(),
        verifier=lambda repo_id, files: "verified",
    )

    # The manifest now reflects the live output_algorithm_revision.
    manifest_text = (data_root / "manifests" / "a.manifest.json").read_text(encoding="utf-8")
    manifest_payload = json.loads(manifest_text)
    assert manifest_payload["output_algorithm_revision"] == current_output_algorithm_revision()
