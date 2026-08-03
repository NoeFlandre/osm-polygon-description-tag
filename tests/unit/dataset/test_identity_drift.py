"""Identity-drift RED tests: each independent identity change forces rebuild.

Each test changes exactly one field of a published manifest and verifies the
orchestrator recognizes the local artifact as no longer reusable. Together
they prove the complete resumability contract is enforced.
"""

from __future__ import annotations

import json
from pathlib import Path

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
from osm_polygon_description_tag.workflow.orchestrator import (
    PUBLICATION_STATE_FILENAME,
    _process_one,
    read_publication_state,
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


def _publish_state(data_root: Path, source_name: str) -> None:
    state = read_publication_state(data_root)
    state.setdefault("published", {})[source_name] = {
        "source_sha256": source_identity_for(data_root.parent / "raw" / source_name).sha256
        if (data_root.parent / "raw" / source_name).is_file()
        else "x" * 64,
        "output_sha256": "y" * 64,
        "output_bytes": 1024,
        "remote_revision": "rev",
        "artifact_identity": "ignored",
        "completed_at": _CLOCK,
    }
    (data_root / PUBLICATION_STATE_FILENAME).write_text(
        json.dumps(state, sort_keys=True, indent=2), encoding="utf-8"
    )


def _seed_complete_artifact(paths: Paths, source_root: Path, data_root: Path) -> None:
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
            schema_version=3,
            geoparquet_version="1.1.0",
            transform_algorithm_version=3,
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


def _overwrite_manifest(data_root: Path, **overrides: object) -> None:
    path = data_root / "manifests" / "a.manifest.json"
    payload = json.loads(path.read_text(encoding="utf-8"))
    for key, value in overrides.items():
        if key in {"source", "output"}:
            payload[key].update(value)  # type: ignore[union-attr]
        else:
            payload[key] = value
    path.write_text(json.dumps(payload, sort_keys=True, indent=2), encoding="utf-8")


def test_source_identity_drift_forces_rebuild(tmp_path: Path) -> None:
    paths, source_root, data_root = _setup_workspace(tmp_path)
    _seed_complete_artifact(paths, source_root, data_root)
    _overwrite_manifest(data_root, source={"sha256": "f" * 64})
    source = Source(
        path=source_root / "a.osm.pbf",
        name="a.osm.pbf",
        output_name="a.parquet",
        size_bytes=0,
        mtime_ns=0,
    )
    outcome = _process_one(source, paths, clock=_frozen_clock, exporter=_fake_exporter())
    assert outcome.status == "built-needs-upload"


def test_output_identity_drift_forces_rebuild(tmp_path: Path) -> None:
    paths, source_root, data_root = _setup_workspace(tmp_path)
    _seed_complete_artifact(paths, source_root, data_root)
    _overwrite_manifest(data_root, output={"sha256": "f" * 64})
    source = Source(
        path=source_root / "a.osm.pbf",
        name="a.osm.pbf",
        output_name="a.parquet",
        size_bytes=0,
        mtime_ns=0,
    )
    outcome = _process_one(source, paths, clock=_frozen_clock, exporter=_fake_exporter())
    assert outcome.status == "built-needs-upload"


def test_manifest_schema_version_drift_forces_rebuild(tmp_path: Path) -> None:
    paths, source_root, data_root = _setup_workspace(tmp_path)
    _seed_complete_artifact(paths, source_root, data_root)
    _overwrite_manifest(data_root, manifest_schema_version=999)
    source = Source(
        path=source_root / "a.osm.pbf",
        name="a.osm.pbf",
        output_name="a.parquet",
        size_bytes=0,
        mtime_ns=0,
    )
    outcome = _process_one(source, paths, clock=_frozen_clock, exporter=_fake_exporter())
    assert outcome.status == "built-needs-upload"


def test_arrow_schema_version_drift_forces_rebuild(tmp_path: Path) -> None:
    paths, source_root, data_root = _setup_workspace(tmp_path)
    _seed_complete_artifact(paths, source_root, data_root)
    _overwrite_manifest(data_root, schema_version=999)
    source = Source(
        path=source_root / "a.osm.pbf",
        name="a.osm.pbf",
        output_name="a.parquet",
        size_bytes=0,
        mtime_ns=0,
    )
    outcome = _process_one(source, paths, clock=_frozen_clock, exporter=_fake_exporter())
    assert outcome.status == "built-needs-upload"


def test_transform_algorithm_version_drift_forces_rebuild(tmp_path: Path) -> None:
    paths, source_root, data_root = _setup_workspace(tmp_path)
    _seed_complete_artifact(paths, source_root, data_root)
    _overwrite_manifest(data_root, transform_algorithm_version=999)
    source = Source(
        path=source_root / "a.osm.pbf",
        name="a.osm.pbf",
        output_name="a.parquet",
        size_bytes=0,
        mtime_ns=0,
    )
    outcome = _process_one(source, paths, clock=_frozen_clock, exporter=_fake_exporter())
    assert outcome.status == "built-needs-upload"


def test_area_policy_sha_drift_forces_rebuild(tmp_path: Path) -> None:
    paths, source_root, data_root = _setup_workspace(tmp_path)
    _seed_complete_artifact(paths, source_root, data_root)
    _overwrite_manifest(data_root, area_policy_sha256="f" * 64)
    source = Source(
        path=source_root / "a.osm.pbf",
        name="a.osm.pbf",
        output_name="a.parquet",
        size_bytes=0,
        mtime_ns=0,
    )
    outcome = _process_one(source, paths, clock=_frozen_clock, exporter=_fake_exporter())
    assert outcome.status == "built-needs-upload"


def test_state_source_identity_drift_forces_republish(tmp_path: Path) -> None:
    paths, source_root, data_root = _setup_workspace(tmp_path)
    _seed_complete_artifact(paths, source_root, data_root)
    _publish_state(data_root, "a.osm.pbf")
    # Mutate publication state: source_sha256 does not match the live source.
    state = read_publication_state(data_root)
    state["published"]["a.osm.pbf"]["source_sha256"] = "f" * 64
    (data_root / PUBLICATION_STATE_FILENAME).write_text(
        json.dumps(state, sort_keys=True, indent=2), encoding="utf-8"
    )
    report = run_and_publish(
        paths=paths,
        confirm_repo="NoeFlandre/osm-polygon-description-tag",
        preflight=lambda: {"preflight": "stub", "source_count": 1},
        upload_runner=lambda command: "rev",
        clock=_frozen_clock,
        exporter=_fake_exporter(),
        verifier=lambda repo_id, files: "verified",
    )
    assert report.outcomes[0].status in {"reused-local-needs-upload", "built-needs-upload"}
    assert report.outcomes[0].remote_revision == "verified"


def test_state_output_identity_drift_forces_republish(tmp_path: Path) -> None:
    paths, source_root, data_root = _setup_workspace(tmp_path)
    _seed_complete_artifact(paths, source_root, data_root)
    _publish_state(data_root, "a.osm.pbf")
    state = read_publication_state(data_root)
    state["published"]["a.osm.pbf"]["output_sha256"] = "f" * 64
    (data_root / PUBLICATION_STATE_FILENAME).write_text(
        json.dumps(state, sort_keys=True, indent=2), encoding="utf-8"
    )
    report = run_and_publish(
        paths=paths,
        confirm_repo="NoeFlandre/osm-polygon-description-tag",
        preflight=lambda: {"preflight": "stub", "source_count": 1},
        upload_runner=lambda command: "rev",
        clock=_frozen_clock,
        exporter=_fake_exporter(),
        verifier=lambda repo_id, files: "verified",
    )
    assert report.outcomes[0].remote_revision == "verified"
