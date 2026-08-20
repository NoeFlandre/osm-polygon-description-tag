import json
from pathlib import Path

import pyarrow as pa
import pyarrow.parquet as pq
import pytest
from shapely import to_wkb
from shapely.geometry import Polygon

from osm_polygon_description_tag.dataset.schema import SCHEMA
from osm_polygon_description_tag.dataset.storage import (
    StorageError,
    _validate_area,
    _validate_geometry,
    validate_geoparquet,
    write_geoparquet,
)
from tests.conftest import make_record_dict

_POLYGON_WKB = to_wkb(Polygon([(0, 0), (0, 1), (1, 1), (1, 0)]), output_dimension=2)


def test_failed_validation_never_promotes_output(
    tmp_path: Path, valid_records: list[dict[str, object]]
) -> None:
    target = tmp_path / "region.parquet"

    def fail(_path: Path) -> int:
        raise ValueError("forced validation failure")

    with pytest.raises(ValueError, match="forced validation failure"):
        write_geoparquet(iter(valid_records), target, batch_size=2, validator=fail)

    assert not target.exists()
    assert list(tmp_path.glob("*.tmp")) == []
    assert list(tmp_path.glob(".*.tmp")) == []


def test_write_and_validate_roundtrip_single_batch(
    tmp_path: Path, valid_records: list[dict[str, object]]
) -> None:
    target = tmp_path / "region.parquet"

    rows = write_geoparquet(iter(valid_records), target, batch_size=10)

    assert target.is_file()
    assert rows == len(valid_records)
    assert validate_geoparquet(target) == len(valid_records)


def test_write_multiple_batches(tmp_path: Path) -> None:
    target = tmp_path / "region.parquet"
    # Distinct identities so the per-file uniqueness constraint holds across batches.
    records = [
        make_record_dict(
            Polygon([(i, 0), (i, 1), (i + 0.5, 1), (i + 0.5, 0)]),
            {"description": f"feature {i}"},
            osm_id=1000 + i,
        )
        for i in range(5)
    ]

    rows = write_geoparquet(iter(records), target, batch_size=2)

    assert rows == len(records)
    assert validate_geoparquet(target) == len(records)


def test_write_empty_file_is_valid(tmp_path: Path) -> None:
    target = tmp_path / "empty.parquet"

    rows = write_geoparquet(iter(()), target, batch_size=10)

    assert rows == 0
    assert validate_geoparquet(target) == 0
    geo = json.loads(pq.ParquetFile(target).schema_arrow.metadata[b"geo"])
    assert geo["columns"]["geometry"]["geometry_types"] == []
    assert "bbox" not in geo["columns"]["geometry"]


def test_written_file_has_geoparquet_metadata(
    tmp_path: Path, valid_records: list[dict[str, object]]
) -> None:
    target = tmp_path / "region.parquet"
    write_geoparquet(iter(valid_records), target, batch_size=10)

    schema_meta = pq.ParquetFile(target).schema_arrow.metadata
    geo = json.loads(schema_meta[b"geo"])
    assert geo["version"] == "1.1.0"
    assert geo["primary_column"] == "geometry"
    assert geo["columns"]["geometry"]["encoding"] == "WKB"
    assert sorted(geo["columns"]["geometry"]["geometry_types"]) == ["MultiPolygon", "Polygon"]
    assert "crs" not in geo["columns"]["geometry"]


def test_validate_detects_duplicate_identity(tmp_path: Path) -> None:
    target = tmp_path / "dup.parquet"
    # Two rows with identical (osm_type, osm_id); valid WKB so row 0 fully passes.
    base_row = {
        "source_pbf": "r.osm.pbf",
        "osm_type": "way",
        "osm_id": 5,
        "osm_url": "https://www.openstreetmap.org/way/5",
        "version": 1,
        "changeset": 1,
        "timestamp": None,
        "description": "x",
        "localized_descriptions": [],
        "tags": [{"key": "description", "value": "x"}],
        "geometry_type": "Polygon",
        "area_m2": 1.0,
        "bbox_min_x": 0.0,
        "bbox_min_y": 0.0,
        "bbox_max_x": 1.0,
        "bbox_max_y": 1.0,
        "geometry": _POLYGON_WKB,
    }
    _write_raw(target, [base_row, dict(base_row)])

    with pytest.raises(ValueError, match="duplicate"):
        validate_geoparquet(target)


def test_validate_rejects_wrong_schema(tmp_path: Path) -> None:
    target = tmp_path / "wrong.parquet"
    bad_schema = pa.schema([pa.field("unexpected", pa.string())])
    pq.write_table(pa.table({"unexpected": ["a"]}, schema=bad_schema), target)

    with pytest.raises(ValueError, match="schema"):
        validate_geoparquet(target)


def test_validate_rejects_missing_file(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="missing"):
        validate_geoparquet(tmp_path / "nope.parquet")


def test_validate_detects_bbox_mismatch(tmp_path: Path) -> None:
    target = tmp_path / "bbox.parquet"
    base_row = {
        "source_pbf": "r.osm.pbf",
        "osm_type": "way",
        "osm_id": 1,
        "osm_url": "https://www.openstreetmap.org/way/1",
        "version": 1,
        "changeset": 1,
        "timestamp": None,
        "description": "x",
        "localized_descriptions": [],
        "tags": [{"key": "description", "value": "x"}],
        "geometry_type": "Polygon",
        "area_m2": 1.0,
        "bbox_min_x": 0.0,
        "bbox_min_y": 0.0,
        "bbox_max_x": 1.0,
        "bbox_max_y": 1.0,
        "geometry": _POLYGON_WKB,
    }
    _write_raw_with_meta(target, [base_row], metadata_bbox=[10.0, 10.0, 11.0, 11.0])

    with pytest.raises(StorageError, match="bbox mismatch"):
        validate_geoparquet(target)


def test_validate_detects_geometry_type_drift(tmp_path: Path) -> None:
    target = tmp_path / "drift.parquet"
    base_row = {
        "source_pbf": "r.osm.pbf",
        "osm_type": "way",
        "osm_id": 1,
        "osm_url": "https://www.openstreetmap.org/way/1",
        "version": 1,
        "changeset": 1,
        "timestamp": None,
        "description": "x",
        "localized_descriptions": [],
        "tags": [{"key": "description", "value": "x"}],
        "geometry_type": "Polygon",
        "area_m2": 1.0,
        "bbox_min_x": 0.0,
        "bbox_min_y": 0.0,
        "bbox_max_x": 1.0,
        "bbox_max_y": 1.0,
        "geometry": _POLYGON_WKB,
    }
    _write_raw_with_meta(target, [base_row], metadata_types=["MultiPolygon"])

    with pytest.raises(StorageError, match="geometry_types mismatch"):
        validate_geoparquet(target)


def test_validate_detects_unordered_bbox(tmp_path: Path) -> None:
    target = tmp_path / "order.parquet"
    row = {
        "source_pbf": "r.osm.pbf",
        "osm_type": "way",
        "osm_id": 1,
        "osm_url": "https://www.openstreetmap.org/way/1",
        "version": 1,
        "changeset": 1,
        "timestamp": None,
        "description": "x",
        "localized_descriptions": [],
        "tags": [{"key": "description", "value": "x"}],
        "geometry_type": "Polygon",
        "area_m2": 1.0,
        "bbox_min_x": 5.0,
        "bbox_min_y": 0.0,
        "bbox_max_x": 1.0,
        "bbox_max_y": 1.0,
        "geometry": _POLYGON_WKB,
    }
    _write_raw(target, [row])

    with pytest.raises(StorageError, match="bbox min"):
        validate_geoparquet(target)


def test_validate_detects_undecodable_wkb(tmp_path: Path) -> None:
    target = tmp_path / "badwkb.parquet"
    row = {
        "source_pbf": "r.osm.pbf",
        "osm_type": "way",
        "osm_id": 1,
        "osm_url": "https://www.openstreetmap.org/way/1",
        "version": 1,
        "changeset": 1,
        "timestamp": None,
        "description": "x",
        "localized_descriptions": [],
        "tags": [{"key": "description", "value": "x"}],
        "geometry_type": "Polygon",
        "area_m2": 1.0,
        "bbox_min_x": 0.0,
        "bbox_min_y": 0.0,
        "bbox_max_x": 1.0,
        "bbox_max_y": 1.0,
        "geometry": b"not-a-wkb",
    }
    _write_raw(target, [row])

    with pytest.raises(StorageError, match="undecodable"):
        validate_geoparquet(target)


def test_row_geometry_and_area_helpers_accept_valid_values() -> None:
    _validate_geometry(_POLYGON_WKB, "Polygon")
    _validate_area(1.0)


def test_row_geometry_and_area_helpers_reject_invalid_values() -> None:
    with pytest.raises(StorageError, match="geometry_type/WKB"):
        _validate_geometry(_POLYGON_WKB, "MultiPolygon")
    with pytest.raises(StorageError, match="non-positive"):
        _validate_area(0.0)


def test_validate_detects_mixed_source_pbf(tmp_path: Path) -> None:
    target = tmp_path / "mixed.parquet"
    base = {
        "osm_type": "way",
        "osm_url": "https://www.openstreetmap.org/way/",
        "version": 1,
        "changeset": 1,
        "timestamp": None,
        "description": "x",
        "localized_descriptions": [],
        "tags": [{"key": "description", "value": "x"}],
        "geometry_type": "Polygon",
        "area_m2": 1.0,
        "bbox_min_x": 0.0,
        "bbox_min_y": 0.0,
        "bbox_max_x": 1.0,
        "bbox_max_y": 1.0,
        "geometry": _POLYGON_WKB,
    }
    _write_raw(
        target,
        [
            {
                **base,
                "osm_id": 1,
                "osm_url": "https://www.openstreetmap.org/way/1",
                "source_pbf": "a.osm.pbf",
            },
            {
                **base,
                "osm_id": 2,
                "osm_url": "https://www.openstreetmap.org/way/2",
                "source_pbf": "b.osm.pbf",
            },
        ],
    )

    with pytest.raises(StorageError, match="mixed source_pbf"):
        validate_geoparquet(target)


def _write_raw_with_meta(
    target: Path,
    rows: list[dict[str, object]],
    *,
    metadata_types: list[str] | None = None,
    metadata_bbox: list[float] | None = None,
) -> None:
    table = pa.Table.from_pylist(rows, schema=SCHEMA)
    geo = {
        "version": "1.1.0",
        "primary_column": "geometry",
        "columns": {
            "geometry": {
                "encoding": "WKB",
                "geometry_types": metadata_types or ["Polygon"],
                "bbox": metadata_bbox if metadata_bbox is not None else [0.0, 0.0, 1.0, 1.0],
            }
        },
    }
    schema_geo = SCHEMA.with_metadata({b"geo": json.dumps(geo).encode("utf-8")})
    with pq.ParquetWriter(target, schema_geo, compression="zstd") as writer:
        writer.write_table(table)


def _write_raw(target: Path, rows: list[dict[str, object]]) -> None:
    """Write rows with the frozen schema plus a minimal geo metadata block."""
    table = pa.Table.from_pylist(rows, schema=SCHEMA)
    geo = {
        "version": "1.1.0",
        "primary_column": "geometry",
        "columns": {
            "geometry": {
                "encoding": "WKB",
                "geometry_types": ["Polygon"],
                "bbox": [0.0, 0.0, 1.0, 1.0],
            }
        },
    }
    schema_geo = SCHEMA.with_metadata({b"geo": json.dumps(geo).encode("utf-8")})
    with pq.ParquetWriter(target, schema_geo, compression="zstd") as writer:
        writer.write_table(table)


def test_validate_rejects_wrong_field_type(tmp_path: Path) -> None:
    """Validation rejects a file whose column type does not match the frozen schema."""
    target = tmp_path / "wrong_type.parquet"
    # Build a new schema with osm_id=Int32 but otherwise match SCHEMA.
    fields = [
        pa.field(name, SCHEMA.field(name).type)
        if name != "osm_id"
        else pa.field("osm_id", pa.int32(), nullable=False)
        for name in SCHEMA.names
    ]
    wrong_schema = pa.schema(fields)
    table = pa.Table.from_pylist([], schema=wrong_schema)
    geo = {
        "version": "1.1.0",
        "primary_column": "geometry",
        "columns": {"geometry": {"encoding": "WKB", "geometry_types": []}},
    }
    schema_meta = wrong_schema.with_metadata({b"geo": json.dumps(geo).encode("utf-8")})
    with pq.ParquetWriter(target, schema_meta, compression="zstd") as writer:
        writer.write_table(table)

    with pytest.raises(StorageError, match="type mismatch|field type|nullability"):
        validate_geoparquet(target)


def test_validate_rejects_missing_geo_metadata(tmp_path: Path) -> None:
    target = tmp_path / "no_geo.parquet"
    row = {
        "source_pbf": "r.osm.pbf",
        "osm_type": "way",
        "osm_id": 1,
        "osm_url": "https://www.openstreetmap.org/way/1",
        "version": 1,
        "changeset": 1,
        "timestamp": None,
        "description": "x",
        "localized_descriptions": [],
        "tags": [("description", "x")],
        "geometry_type": "Polygon",
        "area_m2": 1.0,
        "bbox_min_x": 0.0,
        "bbox_min_y": 0.0,
        "bbox_max_x": 1.0,
        "bbox_max_y": 1.0,
        "geometry": _POLYGON_WKB,
    }
    table = pa.Table.from_pylist([row], schema=SCHEMA)
    with pq.ParquetWriter(target, SCHEMA, compression="zstd") as writer:
        writer.write_table(table)

    with pytest.raises(StorageError, match="missing GeoParquet"):
        validate_geoparquet(target)


def test_validate_rejects_corrupt_geo_metadata(tmp_path: Path) -> None:
    target = tmp_path / "corrupt.parquet"
    table = pa.Table.from_pylist([], schema=SCHEMA)
    schema_meta = SCHEMA.with_metadata({b"geo": b"{not json"})
    with pq.ParquetWriter(target, schema_meta, compression="zstd") as writer:
        writer.write_table(table)

    with pytest.raises(StorageError, match="invalid GeoParquet"):
        validate_geoparquet(target)


def test_validate_rejects_wrong_geoparquet_version(tmp_path: Path) -> None:
    target = tmp_path / "version.parquet"
    table = pa.Table.from_pylist([], schema=SCHEMA)
    geo = {
        "version": "1.0.0",
        "primary_column": "geometry",
        "columns": {"geometry": {"encoding": "WKB", "geometry_types": []}},
    }
    schema_meta = SCHEMA.with_metadata({b"geo": json.dumps(geo).encode("utf-8")})
    with pq.ParquetWriter(target, schema_meta, compression="zstd") as writer:
        writer.write_table(table)

    with pytest.raises(StorageError, match="version"):
        validate_geoparquet(target)


def test_validate_rejects_wrong_primary_column(tmp_path: Path) -> None:
    target = tmp_path / "primary.parquet"
    table = pa.Table.from_pylist([], schema=SCHEMA)
    geo = {
        "version": "1.1.0",
        "primary_column": "geom",
        "columns": {"geometry": {"encoding": "WKB", "geometry_types": []}},
    }
    schema_meta = SCHEMA.with_metadata({b"geo": json.dumps(geo).encode("utf-8")})
    with pq.ParquetWriter(target, schema_meta, compression="zstd") as writer:
        writer.write_table(table)

    with pytest.raises(StorageError, match="primary"):
        validate_geoparquet(target)


def test_validate_rejects_wrong_encoding(tmp_path: Path) -> None:
    target = tmp_path / "encoding.parquet"
    table = pa.Table.from_pylist([], schema=SCHEMA)
    geo = {
        "version": "1.1.0",
        "primary_column": "geometry",
        "columns": {"geometry": {"encoding": "WKT", "geometry_types": []}},
    }
    schema_meta = SCHEMA.with_metadata({b"geo": json.dumps(geo).encode("utf-8")})
    with pq.ParquetWriter(target, schema_meta, compression="zstd") as writer:
        writer.write_table(table)

    with pytest.raises(StorageError, match="WKB"):
        validate_geoparquet(target)
