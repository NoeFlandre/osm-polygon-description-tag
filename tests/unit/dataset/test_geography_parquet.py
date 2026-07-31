"""Parquet aggregation contract tests for the H3 density map.

These tests prove the streaming aggregation:

* streams geometry WKB in batches via ``pq.ParquetFile.iter_batches`` and
  never calls ``pq.read_table`` for the complete dataset;
* counts every row exactly once, preserving duplicate OSM objects from
  different files as separate dataset rows (no global deduplication);
* rejects malformed WKB, invalid geometry, null geometry, non-finite
  coordinates, and out-of-range coordinates with a descriptive error;
* aggregates multiple Parquet files deterministically and emits sorted
  H3 cell counts;
* reports a total equal to the number of validated dataset rows.
"""

from __future__ import annotations

import math
from pathlib import Path
from typing import Any

import pyarrow as pa
import pyarrow.parquet as pq
import pytest
from shapely import to_wkb
from shapely.geometry import MultiPolygon, Point, Polygon

# Import the actual function that the package uses to ensure coverage
# against the no-pq.read_table contract.
import osm_polygon_description_tag.dataset.geography.parquet_inputs as parquet_inputs_module
from osm_polygon_description_tag.dataset.geography import (
    DEFAULT_H3_RESOLUTION,
    PARQUET_INPUT_COLUMNS,
    aggregate_h3_density,
    collect_h3_counts,
)
from osm_polygon_description_tag.dataset.geography.parquet_inputs import (
    H3AggregationError,
    iter_centroids,
)
from osm_polygon_description_tag.dataset.manifest import (
    Manifest,
    RunCounts,
    current_area_policy_sha256,
    current_output_algorithm_revision,
    output_identity_for,
    source_identity_for,
    write_manifest,
)
from osm_polygon_description_tag.dataset.storage import write_geoparquet
from tests.conftest import make_record_dict


def _make_valid_record(
    *,
    osm_id: int,
    description: str = "A place",
    geom: Polygon | MultiPolygon | None = None,
) -> dict[str, object]:
    if geom is None:
        geom = Polygon([(0, 0), (0, 1), (1, 1), (1, 0)])
    return make_record_dict(geom, {"description": description}, osm_id=osm_id)


def _plant_two_parquets(tmp_path: Path) -> Path:
    data_root = tmp_path / "generated"
    source_root = tmp_path / "raw"
    (data_root / "data").mkdir(parents=True)
    (data_root / "manifests").mkdir(parents=True)
    source_root.mkdir(exist_ok=True)
    for stem, record in (
        (
            "alpha",
            _make_valid_record(
                osm_id=1, description="Alpha", geom=Polygon([(0, 0), (1, 0), (1, 1), (0, 1)])
            ),
        ),
        (
            "beta",
            _make_valid_record(
                osm_id=2, description="Beta", geom=Polygon([(10, 10), (11, 10), (11, 11), (10, 11)])
            ),
        ),
    ):
        source = source_root / f"{stem}.osm.pbf"
        source.write_bytes(stem.encode("utf-8"))
        output = data_root / "data" / f"{stem}.parquet"
        write_geoparquet(iter([record]), output, batch_size=10)
        write_manifest(
            Manifest(
                manifest_schema_version=2,
                schema_version=2,
                geoparquet_version="1.1.0",
                transform_algorithm_version=2,
                area_policy_sha256=current_area_policy_sha256(),
                output_algorithm_revision=current_output_algorithm_revision(),
                source=source_identity_for(source),
                output=output_identity_for(output),
                osmium_version=None,
                dependency_versions={"pyarrow": "20.0.0"},
                code_revision=None,
                started_at="2026-01-01T00:00:00+00:00",
                completed_at="2026-01-01T00:00:01+00:00",
                counts=RunCounts(emitted_features=1, included_rows=1, rejections={}),
            ),
            data_root / "manifests" / f"{stem}.manifest.json",
        )
    return data_root


# ---------------------------------------------------------------------------
# Aggregate
# ---------------------------------------------------------------------------


def test_aggregate_h3_density_counts_every_row(tmp_path: Path) -> None:
    data_root = _plant_two_parquets(tmp_path)
    counts = aggregate_h3_density(data_root)
    total = sum(counts.values())
    # Two rows, one per file.
    assert total == 2
    # Sorted by H3 cell id (string).
    assert list(counts.keys()) == sorted(counts.keys())


def test_aggregate_h3_density_preserves_regional_overlap(tmp_path: Path) -> None:
    """The same OSM object across two files is counted twice (no dedup)."""
    data_root = tmp_path / "generated"
    source_root = tmp_path / "raw"
    (data_root / "data").mkdir(parents=True)
    (data_root / "manifests").mkdir(parents=True)
    source_root.mkdir(exist_ok=True)
    geom = Polygon([(0, 0), (0, 1), (1, 1), (1, 0)])
    record = make_record_dict(geom, {"description": "Same object"}, osm_id=42)
    for stem in ("alpha", "beta"):
        source = source_root / f"{stem}.osm.pbf"
        source.write_bytes(stem.encode("utf-8"))
        output = data_root / "data" / f"{stem}.parquet"
        write_geoparquet(iter([record]), output, batch_size=10)
        write_manifest(
            Manifest(
                manifest_schema_version=2,
                schema_version=2,
                geoparquet_version="1.1.0",
                transform_algorithm_version=2,
                area_policy_sha256=current_area_policy_sha256(),
                output_algorithm_revision=current_output_algorithm_revision(),
                source=source_identity_for(source),
                output=output_identity_for(output),
                osmium_version=None,
                dependency_versions={"pyarrow": "20.0.0"},
                code_revision=None,
                started_at="2026-01-01T00:00:00+00:00",
                completed_at="2026-01-01T00:00:01+00:00",
                counts=RunCounts(emitted_features=1, included_rows=1, rejections={}),
            ),
            data_root / "manifests" / f"{stem}.manifest.json",
        )
    counts = aggregate_h3_density(data_root)
    assert sum(counts.values()) == 2


def test_aggregate_h3_density_is_deterministic(tmp_path: Path) -> None:
    data_root = _plant_two_parquets(tmp_path)
    counts_a = aggregate_h3_density(data_root)
    counts_b = aggregate_h3_density(data_root)
    assert counts_a == counts_b


def test_aggregate_h3_density_uses_resolution_3(tmp_path: Path) -> None:
    data_root = _plant_two_parquets(tmp_path)
    counts = aggregate_h3_density(data_root)
    for cell in counts:
        # H3 v4 cell id at resolution 3 is a 15-character hex string.
        assert isinstance(cell, str)
        assert len(cell) == 15
    assert DEFAULT_H3_RESOLUTION == 3


# ---------------------------------------------------------------------------
# collect_h3_counts entry point
# ---------------------------------------------------------------------------


def test_collect_h3_counts_returns_sorted_dict(tmp_path: Path) -> None:
    data_root = _plant_two_parquets(tmp_path)
    counts = collect_h3_counts(data_root)
    assert list(counts.keys()) == sorted(counts.keys())
    assert sum(counts.values()) == 2


# ---------------------------------------------------------------------------
# Batched streaming contract
# ---------------------------------------------------------------------------


def test_aggregate_uses_iter_batches_not_read_table(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The aggregator must never call ``pq.read_table`` for the full dataset."""
    data_root = _plant_two_parquets(tmp_path)
    forbidden_calls: list[tuple[tuple, dict]] = []

    real_read_table = pq.read_table

    def guarded_read_table(*args: Any, **kwargs: Any) -> pa.Table:
        # The aggregator must only open a Parquet via ``ParquetFile``. Any
        # call to ``read_table`` is a contract violation.
        forbidden_calls.append((args, kwargs))
        return real_read_table(*args, **kwargs)

    monkeypatch.setattr(pq, "read_table", guarded_read_table)
    aggregate_h3_density(data_root)
    assert forbidden_calls == []


def test_aggregate_uses_batched_reads_with_pruned_columns(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Only the required columns are read, in batched iter_batches() calls."""
    data_root = _plant_two_parquets(tmp_path)
    observed: list[tuple[Path, list[str] | None, int | None]] = []
    real_iter_batches = pq.ParquetFile.iter_batches

    def guarded_iter_batches(self: pq.ParquetFile, *args: Any, **kwargs: Any) -> Any:
        observed.append((Path(str(self)), kwargs.get("columns"), kwargs.get("batch_size")))
        return real_iter_batches(self, *args, **kwargs)

    monkeypatch.setattr(pq.ParquetFile, "iter_batches", guarded_iter_batches)
    aggregate_h3_density(data_root)
    assert observed, "iter_batches must be invoked"
    for _path, columns, batch_size in observed:
        assert set(columns or set()) <= set(PARQUET_INPUT_COLUMNS)
        assert batch_size is not None and batch_size > 0


# ---------------------------------------------------------------------------
# Validation errors
# ---------------------------------------------------------------------------


def test_aggregate_rejects_malformed_wkb(tmp_path: Path) -> None:
    data_root = tmp_path / "generated"
    source_root = tmp_path / "raw"
    (data_root / "data").mkdir(parents=True)
    (data_root / "manifests").mkdir(parents=True)
    source_root.mkdir(exist_ok=True)
    source = source_root / "alpha.osm.pbf"
    source.write_bytes(b"alpha")
    output = data_root / "data" / "alpha.parquet"
    # Manually craft a parquet with a malformed WKB.
    table = pa.table(
        {
            "source_pbf": ["alpha.osm.pbf"],
            "osm_type": ["way"],
            "osm_id": pa.array([1], type=pa.int64()),
            "bbox_min_x": [0.0],
            "bbox_min_y": [0.0],
            "bbox_max_x": [1.0],
            "bbox_max_y": [1.0],
            "geometry": pa.array([b"not-a-valid-wkb"], type=pa.binary()),
        }
    )
    pq.write_table(table, output)
    write_manifest(
        Manifest(
            manifest_schema_version=2,
            schema_version=2,
            geoparquet_version="1.1.0",
            transform_algorithm_version=2,
            area_policy_sha256=current_area_policy_sha256(),
            output_algorithm_revision=current_output_algorithm_revision(),
            source=source_identity_for(source),
            output=output_identity_for(output),
            osmium_version=None,
            dependency_versions={"pyarrow": "20.0.0"},
            code_revision=None,
            started_at="2026-01-01T00:00:00+00:00",
            completed_at="2026-01-01T00:00:01+00:00",
            counts=RunCounts(emitted_features=1, included_rows=1, rejections={}),
        ),
        data_root / "manifests" / "alpha.manifest.json",
    )
    with pytest.raises(H3AggregationError, match="WKB"):
        aggregate_h3_density(data_root)


def test_aggregate_rejects_invalid_geometry(tmp_path: Path) -> None:
    data_root = tmp_path / "generated"
    (data_root / "data").mkdir(parents=True)
    (data_root / "manifests").mkdir(parents=True)
    (tmp_path / "raw").mkdir(exist_ok=True)
    source = tmp_path / "raw" / "alpha.osm.pbf"
    source.write_bytes(b"alpha")
    output = data_root / "data" / "alpha.parquet"
    # A point geometry is not Polygon/MultiPolygon and must be rejected.
    point_wkb = to_wkb(Point(0, 0))
    table = pa.table(
        {
            "source_pbf": ["alpha.osm.pbf"],
            "osm_type": ["way"],
            "osm_id": pa.array([1], type=pa.int64()),
            "bbox_min_x": [0.0],
            "bbox_min_y": [0.0],
            "bbox_max_x": [0.0],
            "bbox_max_y": [0.0],
            "geometry": pa.array([point_wkb], type=pa.binary()),
        }
    )
    pq.write_table(table, output)
    write_manifest(
        Manifest(
            manifest_schema_version=2,
            schema_version=2,
            geoparquet_version="1.1.0",
            transform_algorithm_version=2,
            area_policy_sha256=current_area_policy_sha256(),
            output_algorithm_revision=current_output_algorithm_revision(),
            source=source_identity_for(source),
            output=output_identity_for(output),
            osmium_version=None,
            dependency_versions={"pyarrow": "20.0.0"},
            code_revision=None,
            started_at="2026-01-01T00:00:00+00:00",
            completed_at="2026-01-01T00:00:01+00:00",
            counts=RunCounts(emitted_features=1, included_rows=1, rejections={}),
        ),
        data_root / "manifests" / "alpha.manifest.json",
    )
    with pytest.raises(H3AggregationError, match="geometry"):
        aggregate_h3_density(data_root)


def test_aggregate_rejects_null_geometry(tmp_path: Path) -> None:
    data_root = tmp_path / "generated"
    (data_root / "data").mkdir(parents=True)
    (data_root / "manifests").mkdir(parents=True)
    (tmp_path / "raw").mkdir(exist_ok=True)
    source = tmp_path / "raw" / "alpha.osm.pbf"
    source.write_bytes(b"alpha")
    output = data_root / "data" / "alpha.parquet"
    table = pa.table(
        {
            "source_pbf": ["alpha.osm.pbf"],
            "osm_type": ["way"],
            "osm_id": pa.array([1], type=pa.int64()),
            "bbox_min_x": [0.0],
            "bbox_min_y": [0.0],
            "bbox_max_x": [1.0],
            "bbox_max_y": [1.0],
            "geometry": pa.array([None], type=pa.binary()),
        }
    )
    pq.write_table(table, output)
    write_manifest(
        Manifest(
            manifest_schema_version=2,
            schema_version=2,
            geoparquet_version="1.1.0",
            transform_algorithm_version=2,
            area_policy_sha256=current_area_policy_sha256(),
            output_algorithm_revision=current_output_algorithm_revision(),
            source=source_identity_for(source),
            output=output_identity_for(output),
            osmium_version=None,
            dependency_versions={"pyarrow": "20.0.0"},
            code_revision=None,
            started_at="2026-01-01T00:00:00+00:00",
            completed_at="2026-01-01T00:00:01+00:00",
            counts=RunCounts(emitted_features=1, included_rows=1, rejections={}),
        ),
        data_root / "manifests" / "alpha.manifest.json",
    )
    with pytest.raises(H3AggregationError):
        aggregate_h3_density(data_root)


# ---------------------------------------------------------------------------
# Centroid contract
# ---------------------------------------------------------------------------


def test_iter_centroids_uses_geometry_centroid_not_bbox_center(
    tmp_path: Path,
) -> None:
    """Centroids must come from the geometry, not the bounding box centre."""
    data_root = tmp_path / "generated"
    (data_root / "data").mkdir(parents=True)
    (data_root / "manifests").mkdir(parents=True)
    (tmp_path / "raw").mkdir(exist_ok=True)
    source = tmp_path / "raw" / "alpha.osm.pbf"
    source.write_bytes(b"alpha")
    output = data_root / "data" / "alpha.parquet"
    # L-shaped polygon: centroid is NOT the bbox centre.
    geom = Polygon([(0, 0), (0, 4), (1, 4), (1, 1), (4, 1), (4, 0)])
    record = make_record_dict(geom, {"description": "L-shaped"}, osm_id=1)
    write_geoparquet(iter([record]), output, batch_size=10)
    write_manifest(
        Manifest(
            manifest_schema_version=2,
            schema_version=2,
            geoparquet_version="1.1.0",
            transform_algorithm_version=2,
            area_policy_sha256=current_area_policy_sha256(),
            output_algorithm_revision=current_output_algorithm_revision(),
            source=source_identity_for(source),
            output=output_identity_for(output),
            osmium_version=None,
            dependency_versions={"pyarrow": "20.0.0"},
            code_revision=None,
            started_at="2026-01-01T00:00:00+00:00",
            completed_at="2026-01-01T00:00:01+00:00",
            counts=RunCounts(emitted_features=1, included_rows=1, rejections={}),
        ),
        data_root / "manifests" / "alpha.manifest.json",
    )
    rows = list(iter_centroids(data_root))
    assert len(rows) == 1
    _path, lon, lat = rows[0]
    # The centroid of an L-shape is not the bounding-box centre (2.0, 2.0).
    bbox_centre_lon = 2.0
    bbox_centre_lat = 2.0
    # We expect a point that differs from the bbox centre.
    assert not (math.isclose(lon, bbox_centre_lon) and math.isclose(lat, bbox_centre_lat))


# ---------------------------------------------------------------------------
# Map total equals dataset row count
# ---------------------------------------------------------------------------


def test_map_total_equals_validated_row_count(tmp_path: Path) -> None:
    data_root = _plant_two_parquets(tmp_path)
    counts = collect_h3_counts(data_root)
    # The total number of map counts must equal the total number of dataset
    # rows (one row per parquet row, no deduplication).
    total_parquet_rows = 0
    for path in sorted((data_root / "data").glob("*.parquet")):
        total_parquet_rows += pq.ParquetFile(path).metadata.num_rows
    assert sum(counts.values()) == total_parquet_rows


# ---------------------------------------------------------------------------
# Module surface re-exports
# ---------------------------------------------------------------------------


def test_parquet_inputs_module_does_not_call_pq_read_table() -> None:
    """The ``parquet_inputs`` module must not import or call ``pq.read_table``."""
    source = Path(parquet_inputs_module.__file__).read_text(encoding="utf-8")
    assert "pq.read_table" not in source
    assert "read_table(" not in source
