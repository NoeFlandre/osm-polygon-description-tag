"""RED tests for deterministic cross-PBF OSM identity deduplication."""

from pathlib import Path

import pytest
from shapely.geometry import Polygon

from osm_polygon_description_tag.dataset.deduplication import (
    DUPLICATE_REJECTION_REASON,
    deduplicate_dataset,
    select_canonical_row,
)
from osm_polygon_description_tag.dataset.manifest import (
    Manifest,
    RunCounts,
    output_identity_for,
    source_identity_for,
    write_manifest,
)
from osm_polygon_description_tag.dataset.storage import write_geoparquet
from tests.conftest import make_record_dict


def _write_source(
    data_root: Path,
    source_root: Path,
    name: str,
    records: list[dict[str, object]],
) -> None:
    source = source_root / f"{name}.osm.pbf"
    source.write_bytes(name.encode())
    output = data_root / "data" / f"{name}.parquet"
    manifest_path = data_root / "manifests" / f"{name}.manifest.json"
    rows = write_geoparquet(records, output)
    write_manifest(
        Manifest(
            manifest_schema_version=2,
            schema_version=3,
            geoparquet_version="1.1.0",
            transform_algorithm_version=3,
            area_policy_sha256="0" * 64,
            output_algorithm_revision="x" * 64,
            source=source_identity_for(source),
            output=output_identity_for(output),
            osmium_version="osmium version test",
            dependency_versions={},
            code_revision=None,
            started_at="2026-01-01T00:00:00+00:00",
            completed_at="2026-01-01T00:00:00+00:00",
            counts=RunCounts(emitted_features=rows, included_rows=rows, rejections={}),
        ),
        manifest_path,
    )


def test_select_canonical_row_prefers_latest_osm_version_then_filename() -> None:
    rows = [
        {
            "osm_type": "way",
            "osm_id": 1,
            "version": 4,
            "timestamp": None,
            "source_pbf": "z.osm.pbf",
        },
        {
            "osm_type": "way",
            "osm_id": 1,
            "version": 5,
            "timestamp": None,
            "source_pbf": "z.osm.pbf",
        },
        {
            "osm_type": "way",
            "osm_id": 1,
            "version": 5,
            "timestamp": None,
            "source_pbf": "a.osm.pbf",
        },
    ]

    assert select_canonical_row(rows)["source_pbf"] == "a.osm.pbf"
    assert select_canonical_row(rows)["version"] == 5


def test_deduplicate_dataset_rewrites_overlapping_rows_and_is_idempotent(tmp_path: Path) -> None:
    data_root = tmp_path / "generated"
    source_root = tmp_path / "raw"
    (data_root / "data").mkdir(parents=True)
    (data_root / "manifests").mkdir()
    source_root.mkdir()
    duplicate_a = make_record_dict(
        Polygon([(0, 0), (0, 1), (1, 1), (1, 0)]),
        {"description": "old"},
        osm_id=1,
        source_pbf="a.osm.pbf",
    )
    duplicate_b = dict(duplicate_a, source_pbf="b.osm.pbf", version=2, description="new")
    unique = make_record_dict(
        Polygon([(2, 2), (2, 3), (3, 3), (3, 2)]),
        {"description": "unique"},
        osm_id=2,
        source_pbf="b.osm.pbf",
    )
    _write_source(data_root, source_root, "a", [duplicate_a])
    _write_source(data_root, source_root, "b", [duplicate_b, unique])

    result = deduplicate_dataset(data_root)
    assert result.input_rows == 3
    assert result.output_rows == 2
    assert result.duplicate_rows == 1
    assert result.files_changed == 1

    import pyarrow.parquet as pq

    assert pq.read_table(data_root / "data" / "a.parquet").num_rows == 0
    table_b = pq.read_table(data_root / "data" / "b.parquet")
    assert table_b.num_rows == 2
    manifest_a = Manifest.from_payload(
        __import__("json").loads((data_root / "manifests" / "a.manifest.json").read_text())
    )
    assert manifest_a.counts.rejections == {DUPLICATE_REJECTION_REASON: 1}

    second = deduplicate_dataset(data_root)
    assert second.status == "skipped"
    assert second.output_rows == 2


def test_deduplicate_dataset_resumes_after_promotion_interrupt(tmp_path: Path) -> None:
    data_root = tmp_path / "generated"
    source_root = tmp_path / "raw"
    (data_root / "data").mkdir(parents=True)
    (data_root / "manifests").mkdir()
    source_root.mkdir()
    first = make_record_dict(
        Polygon([(0, 0), (0, 1), (1, 1), (1, 0)]),
        {"description": "one"},
        osm_id=1,
        source_pbf="a.osm.pbf",
    )
    duplicate = dict(first, source_pbf="b.osm.pbf", version=2)
    _write_source(data_root, source_root, "a", [first])
    _write_source(data_root, source_root, "b", [duplicate])

    def interrupt(count: int) -> None:
        if count == 1:
            raise KeyboardInterrupt

    with pytest.raises(KeyboardInterrupt):
        deduplicate_dataset(data_root, promotion_hook=interrupt)

    resumed = deduplicate_dataset(data_root)
    assert resumed.status == "deduplicated"
    assert resumed.output_rows == 1
