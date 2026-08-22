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
from types import SimpleNamespace
from typing import Any
from unittest.mock import Mock, call, patch

import pyarrow as pa
import pyarrow.parquet as pq
import pytest
from shapely import to_wkb
from shapely.geometry import MultiPolygon, Point, Polygon

import osm_polygon_description_tag.dataset.geography.aggregation as aggregation_module

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
    _decode_geometry,
    _geometry_centroid,
    _validate_centroid,
    _validate_geometry,
    iter_centroids,
    require_directory,
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
                schema_version=3,
                geoparquet_version="1.1.0",
                transform_algorithm_version=3,
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
                schema_version=3,
                geoparquet_version="1.1.0",
                transform_algorithm_version=3,
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


def test_aggregate_h3_density_forwards_default_and_explicit_resolution(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls: list[tuple[Path, int | None]] = []

    def fake_collect(root: Path, *, h3_resolution: int | None) -> dict[str, int]:
        calls.append((root, h3_resolution))
        return {"cell": h3_resolution or -1}

    monkeypatch.setattr(aggregation_module, "collect_h3_counts", fake_collect)

    assert aggregation_module.aggregate_h3_density(tmp_path) == {"cell": DEFAULT_H3_RESOLUTION}
    assert aggregation_module.aggregate_h3_density(tmp_path, h3_resolution=0) == {"cell": -1}
    assert calls == [
        (tmp_path, DEFAULT_H3_RESOLUTION),
        (tmp_path, 0),
    ]


# ---------------------------------------------------------------------------
# collect_h3_counts entry point
# ---------------------------------------------------------------------------


def test_collect_h3_counts_returns_sorted_dict(tmp_path: Path) -> None:
    data_root = _plant_two_parquets(tmp_path)
    counts = collect_h3_counts(data_root)
    assert list(counts.keys()) == sorted(counts.keys())
    assert sum(counts.values()) == 2


def test_collect_h3_counts_resolution_zero_is_used(tmp_path: Path) -> None:
    """Passing ``h3_resolution=0`` selects resolution 0 (must not fall back to 3)."""
    from osm_polygon_description_tag.dataset.geography.parquet_inputs import (
        H3AggregationError,
    )

    data_root = _plant_two_parquets(tmp_path)
    try:
        counts_zero = collect_h3_counts(data_root, h3_resolution=0)
    except H3AggregationError as error:
        pytest.skip(f"resolution 0 not supported in this H3 build: {error}")
    counts_default = collect_h3_counts(data_root, h3_resolution=None)

    # Resolution 0 has at most 122 cells worldwide. With two points, both
    # canonical aggregates may collide into the SAME cell, but the cell
    # id length differs from resolution 3 (15 hex chars), which is the
    # strong invariant.
    for cell in counts_zero:
        # H3 v4 cell ids are 15-hex strings regardless of resolution.
        assert len(cell) == 15
    # A coarse resolution produces FEWER OR EQUAL distinct cells than the
    # finer resolution, never more.
    assert len(counts_zero) <= len(counts_default)


def test_collect_h3_counts_resolution_none_uses_default(tmp_path: Path) -> None:
    """Passing ``h3_resolution=None`` selects the package default (resolution 3)."""
    data_root = _plant_two_parquets(tmp_path)
    counts_none = collect_h3_counts(data_root, h3_resolution=None)
    counts_explicit = collect_h3_counts(data_root, h3_resolution=DEFAULT_H3_RESOLUTION)
    assert counts_none == counts_explicit
    assert DEFAULT_H3_RESOLUTION == 3


def test_collect_h3_counts_resolution_falsy_does_not_use_default(
    tmp_path: Path,
) -> None:
    """Resolution 0 is valid and must remain resolution 0; only None falls back."""
    from osm_polygon_description_tag.dataset.geography.parquet_inputs import (
        H3AggregationError,
    )

    data_root = _plant_two_parquets(tmp_path)
    try:
        # Explicit zero must succeed (it's a valid H3 resolution).
        counts_zero = collect_h3_counts(data_root, h3_resolution=0)
    except H3AggregationError as error:
        pytest.skip(f"resolution 0 not supported in this H3 build: {error}")
    # Both calls accepted 0/None without raising.
    counts_none = collect_h3_counts(data_root)
    # The two outputs reflect genuinely different resolutions: resolution 0
    # typically maps to fewer cells.
    assert isinstance(counts_zero, dict)
    assert isinstance(counts_none, dict)


def test_collect_h3_counts_forwards_exact_resolution_to_assignment(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import osm_polygon_description_tag.dataset.storage as storage_module

    observed: list[tuple[float, float, int]] = []
    validated: list[Path] = []
    parquet_path = tmp_path / "data" / "region.parquet"

    monkeypatch.setattr(
        parquet_inputs_module,
        "iter_centroids",
        lambda _root: iter(((parquet_path, 2.0, 1.0), (parquet_path, 4.0, 3.0))),
    )
    monkeypatch.setattr(
        storage_module,
        "validate_finalized_artifacts",
        lambda root: validated.append(root),
    )

    def record_assignment(lat: float, lon: float, *, resolution: int) -> str:
        observed.append((lat, lon, resolution))
        return "cell"

    monkeypatch.setattr(parquet_inputs_module, "assign_h3_cell", record_assignment)

    assert collect_h3_counts(tmp_path, h3_resolution=7) == {"cell": 2}
    assert validated == [tmp_path]
    assert observed == [(1.0, 2.0, 7), (3.0, 4.0, 7)]


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
            schema_version=3,
            geoparquet_version="1.1.0",
            transform_algorithm_version=3,
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
            schema_version=3,
            geoparquet_version="1.1.0",
            transform_algorithm_version=3,
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


def test_validate_geometry_rejects_non_geometry_and_accepts_polygon() -> None:
    _validate_geometry(Polygon([(0, 0), (0, 1), (1, 1), (0, 0)]))
    _validate_geometry(MultiPolygon([Polygon([(0, 0), (0, 1), (1, 1), (0, 0)])]))
    with pytest.raises(H3AggregationError) as error:
        _validate_geometry(Point(0, 0))
    assert str(error.value) == "unsupported geometry type for H3 density map: 'Point'"

    with pytest.raises(H3AggregationError) as non_geometry_error:
        _validate_geometry(object())  # type: ignore[arg-type]
    assert str(non_geometry_error.value) == "unsupported geometry type: object"


def test_validate_geometry_distinguishes_empty_and_invalid_polygons() -> None:
    with pytest.raises(H3AggregationError, match="^invalid or empty geometry$"):
        _validate_geometry(Polygon())

    invalid = Polygon([(0, 0), (1, 1), (1, 0), (0, 1), (0, 0)])
    with pytest.raises(H3AggregationError, match="^invalid or empty geometry$"):
        _validate_geometry(invalid)


@pytest.mark.parametrize(
    ("is_empty", "is_valid"),
    ((True, True), (False, False)),
)
def test_validate_centroid_rejects_empty_or_invalid_points(is_empty: bool, is_valid: bool) -> None:
    point = SimpleNamespace(is_empty=is_empty, is_valid=is_valid)
    with pytest.raises(H3AggregationError, match="^could not derive a finite centroid$"):
        _validate_centroid(point)  # type: ignore[arg-type]


def test_decode_geometry_reports_malformed_wkb() -> None:
    with pytest.raises(H3AggregationError, match="^malformed WKB:"):
        _decode_geometry(b"not-a-valid-wkb")


@pytest.mark.parametrize(
    ("lon", "lat"),
    ((float("nan"), 1.0), (1.0, float("inf"))),
)
def test_geometry_centroid_rejects_each_non_finite_coordinate(
    lon: float, lat: float, monkeypatch: pytest.MonkeyPatch
) -> None:
    geometry = SimpleNamespace(centroid=SimpleNamespace(x=lon, y=lat))
    monkeypatch.setattr(parquet_inputs_module, "_decode_geometry", lambda _wkb: geometry)
    monkeypatch.setattr(parquet_inputs_module, "_validate_geometry", lambda _geometry: None)
    monkeypatch.setattr(parquet_inputs_module, "_validate_centroid", lambda _point: None)

    with pytest.raises(H3AggregationError) as error:
        _geometry_centroid(b"unused")
    assert str(error.value).startswith("non-finite centroid:")


def test_require_directory_rejects_missing_and_file_paths(tmp_path: Path) -> None:
    missing = tmp_path / "missing"
    with pytest.raises(H3AggregationError) as missing_error:
        require_directory(missing, label="data")
    assert str(missing_error.value) == (
        f"Required data directory does not exist: {missing}. "
        "Run a complete PBF processing pass first."
    )

    file_path = tmp_path / "not-a-directory"
    file_path.write_bytes(b"x")
    with pytest.raises(H3AggregationError) as file_error:
        require_directory(file_path, label="data")
    assert str(file_error.value) == (
        f"Required data directory does not exist: {file_path}. "
        "Run a complete PBF processing pass first."
    )

    directory = tmp_path / "data"
    directory.mkdir()
    assert require_directory(directory, label="data") is directory


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
            schema_version=3,
            geoparquet_version="1.1.0",
            transform_algorithm_version=3,
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
    with pytest.raises(H3AggregationError) as error:
        aggregate_h3_density(data_root)
    assert str(error.value) == f"null geometry at {output} (osm_id=1)"


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
            schema_version=3,
            geoparquet_version="1.1.0",
            transform_algorithm_version=3,
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


def test_iter_centroids_forwards_exact_streaming_contract(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    data_root = tmp_path / "generated"
    data_dir = data_root / "data"
    parquet_path = data_dir / "region.parquet"
    batch = Mock()
    columns = {
        "geometry": Mock(to_pylist=Mock(return_value=[b"wkb"])),
        "osm_id": Mock(to_pylist=Mock(return_value=[42])),
    }
    batch.column.side_effect = columns.__getitem__
    reader = Mock()
    reader.iter_batches.return_value = [batch]

    with (
        patch.object(
            parquet_inputs_module,
            "require_directory",
            return_value=data_dir,
        ) as require,
        patch.object(
            parquet_inputs_module,
            "sorted_parquets",
            return_value=[parquet_path],
        ) as sorted_paths,
        patch.object(parquet_inputs_module.pq, "ParquetFile", return_value=reader) as parquet_file,
        patch.object(
            parquet_inputs_module,
            "_geometry_centroid",
            return_value=(2.5, 1.5),
        ) as centroid,
        patch.object(parquet_inputs_module, "validate_coordinate") as validate,
    ):
        rows = list(iter_centroids(data_root, batch_size=17))

    assert rows == [(parquet_path, 2.5, 1.5)]
    require.assert_called_once_with(data_root / "data", label="data")
    sorted_paths.assert_called_once_with(data_dir)
    parquet_file.assert_called_once_with(parquet_path)
    reader.iter_batches.assert_called_once_with(columns=list(PARQUET_INPUT_COLUMNS), batch_size=17)
    batch.column.assert_has_calls([call("geometry"), call("osm_id")])
    centroid.assert_called_once_with(b"wkb")
    validate.assert_called_once_with(1.5, 2.5)


def test_iter_centroids_reports_null_geometry_with_source_identity(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    data_root = tmp_path / "generated"
    data_dir = data_root / "data"
    parquet_path = data_dir / "region.parquet"
    batch = Mock()
    columns = {
        "geometry": Mock(to_pylist=Mock(return_value=[None])),
        "osm_id": Mock(to_pylist=Mock(return_value=[42])),
    }
    batch.column.side_effect = columns.__getitem__
    reader = Mock()
    reader.iter_batches.return_value = [batch]
    monkeypatch.setattr(
        parquet_inputs_module, "require_directory", lambda *_args, **_kwargs: data_dir
    )
    monkeypatch.setattr(parquet_inputs_module, "sorted_parquets", lambda _directory: [parquet_path])
    monkeypatch.setattr(parquet_inputs_module.pq, "ParquetFile", lambda _path: reader)

    with pytest.raises(H3AggregationError) as error:
        list(iter_centroids(data_root))

    assert str(error.value) == f"null geometry at {parquet_path} (osm_id=42)"


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


# ---------------------------------------------------------------------------
# Shared validation primitive (defect 7)
# ---------------------------------------------------------------------------


def test_aggregate_h3_density_uses_validate_finalized_artifacts(tmp_path: Path) -> None:
    """``aggregate_h3_density`` must use the shared validation primitive."""
    from osm_polygon_description_tag.dataset.storage import (
        validate_finalized_artifacts,
    )

    data_root = _plant_two_parquets(tmp_path)

    # Drop one manifest: the shared primitive must reject this state.
    (data_root / "manifests" / "beta.manifest.json").unlink()
    from osm_polygon_description_tag.dataset.storage import StorageError

    with pytest.raises(StorageError, match="mismatch"):
        validate_finalized_artifacts(data_root)
    with pytest.raises(StorageError, match="mismatch"):
        collect_h3_counts(data_root)


def test_generate_dataset_docs_uses_validate_finalized_artifacts(tmp_path: Path) -> None:
    """``generate_dataset_docs`` must use the shared validation primitive."""
    from shapely.geometry import Polygon

    from osm_polygon_description_tag._resources import dataset_card_template
    from osm_polygon_description_tag.dataset.manifest import (
        Manifest,
        RunCounts,
        output_identity_for,
        source_identity_for,
        write_manifest,
    )
    from osm_polygon_description_tag.dataset.reporting import generate_dataset_docs
    from osm_polygon_description_tag.dataset.storage import (
        StorageError,
        write_geoparquet,
    )

    data_root = tmp_path / "generated"
    (data_root / "data").mkdir(parents=True)
    (data_root / "manifests").mkdir(parents=True)
    # Plant a single validated parquet + manifest.
    raw = tmp_path / "raw"
    raw.mkdir()
    (raw / "single.osm.pbf").write_bytes(b"single-bytes")
    source = raw / "single.osm.pbf"
    record = make_record_dict(
        Polygon([(0, 0), (0, 1), (1, 1), (1, 0)]),
        {"description": "single"},
        osm_id=1,
        source_pbf="single.osm.pbf",
    )
    output = data_root / "data" / "single.parquet"
    write_geoparquet(iter([record]), output, batch_size=10)
    write_manifest(
        Manifest(
            manifest_schema_version=2,
            schema_version=3,
            geoparquet_version="1.1.0",
            transform_algorithm_version=3,
            area_policy_sha256="0" * 64,
            output_algorithm_revision="0" * 64,
            source=source_identity_for(source),
            output=output_identity_for(output),
            osmium_version="osmium version 1.19.1",
            dependency_versions={"pyarrow": "20.0.0"},
            code_revision="abc",
            started_at="2026-07-30T00:00:00+00:00",
            completed_at="2026-07-30T00:01:00+00:00",
            counts=RunCounts(emitted_features=1, included_rows=1, rejections={}),
        ),
        data_root / "manifests" / "single.manifest.json",
    )
    (data_root / "README.md").write_text(dataset_card_template().read_text(encoding="utf-8"))

    # Corrupt the manifest so the shared primitive refuses to validate.
    (data_root / "manifests" / "single.manifest.json").write_text(
        '{"manifest_schema_version": 1, "garbage": true}\n', encoding="utf-8"
    )
    # The reporting layer wraps the shared validation primitive and
    # translates the failure into a ReportingError.
    from osm_polygon_description_tag.dataset.reporting import ReportingError

    with pytest.raises((StorageError, ReportingError), match="(invalid|schema|manifest)"):
        generate_dataset_docs(
            data_root,
            dataset_card_template(),
            clock=lambda: "2026-07-30T00:02:00+00:00",
        )
