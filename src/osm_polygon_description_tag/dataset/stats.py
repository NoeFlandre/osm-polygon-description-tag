"""Artifact-derived dataset statistics.

Statistics read only validated Parquet files with matching manifests and are
computed from the finalized artifacts. Handwritten numeric claims are never
introduced here.

Bounded memory: aggregate statistics are computed by streaming each Parquet
file into an in-memory DuckDB instance using per-batch Arrow ingestion. Exact
area quantiles use ``quantile_cont`` on the DuckDB-backed ``area_m2`` column;
the additional area, extent, and geometry-complexity pass reads one batch at a
time, so it does not retain the dataset's WKB in Python.

"""

from __future__ import annotations

import math
from collections.abc import Callable
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any, cast

import duckdb
import pyarrow as pa
import pyarrow.parquet as pq
from shapely import from_wkb
from shapely.errors import ShapelyError
from shapely.geometry import MultiPolygon, Polygon
from shapely.geometry.base import BaseGeometry

from osm_polygon_description_tag.dataset.canonical_rows import canonical_geometry_wkb_sql
from osm_polygon_description_tag.dataset.manifest import (
    Manifest,
    ManifestError,
    _manifest_path_for,
    file_sha256,
    output_identity_for,
    read_manifest,
)
from osm_polygon_description_tag.dataset.schema import SCHEMA, SCHEMA_VERSION
from osm_polygon_description_tag.dataset.storage import validate_geoparquet
from osm_polygon_description_tag.dataset.text import successful_description_text_sql
from osm_polygon_description_tag.dataset.unique_rows import (
    iter_unique_parquet_batches,
    unique_rows_sql,
)
from osm_polygon_description_tag.runtime.time import utc_now_iso

STATS_SCHEMA_VERSION = 9
_QUANTILE_PROBABILITIES = [0.25, 0.5, 0.75]
TEXT_REJECTION_REASONS = (
    "no_description",
    "missing_description",
    "no_nonempty_description",
    "blank_description",
    "malformed_description",
    "failed_description_extraction",
)
_FEATURE_COLUMNS = list(SCHEMA.names)
_SPATIAL_COLUMNS = [
    "source_pbf",
    "osm_type",
    "osm_id",
    "geometry_type",
    "area_m2",
    "bbox_min_x",
    "bbox_min_y",
    "bbox_max_x",
    "bbox_max_y",
    "geometry",
]


class ReportingError(ValueError):
    """Raised when artifacts/manifests are missing, stale, or inconsistent."""


def _new_connection(data_root: Path) -> duckdb.DuckDBPyConnection:
    work_root = data_root / ".work" / "duckdb"
    work_root.mkdir(parents=True, exist_ok=True)
    connection = duckdb.connect(":memory:")
    connection.execute("SET temp_directory = ?", [str(work_root)])
    return connection


def _quantile_or_none(
    connection: duckdb.DuckDBPyConnection,
    column: str,
    probability: float,
) -> float | None:
    if column != "area_m2":
        return None
    query = (
        f"SELECT quantile_cont({column}, {probability}) FROM features WHERE {column} IS NOT NULL"  # noqa: S608
    )
    result = connection.execute(query).fetchone()
    if result is None or result[0] is None:
        return None
    return float(result[0])


def _suffix_counts(connection: duckdb.DuckDBPyConnection, map_column: str) -> dict[str, int]:
    if rows := connection.execute(
        f"""
        SELECT entry.key AS suffix, COUNT(*) AS value FROM (
            SELECT unnest(map_entries({map_column})) AS entry FROM features
            WHERE cardinality({map_column}) > 0
        ) GROUP BY entry.key ORDER BY entry.key
        """  # noqa: S608 - map_column is allowlisted by the caller
    ).fetchall():
        return {key: int(value) for key, value in rows}
    return {}


def _map_sql_expression(batch: pa.RecordBatch, column: str) -> str:
    """Normalize legacy Arrow maps and Hub-compatible key/value lists in SQL."""
    field = batch.schema.field(column)
    if pa.types.is_map(field.type):
        return column
    if pa.types.is_list(field.type):
        return f"map_from_entries({column})"
    raise ReportingError(f"unsupported mapping representation for {column}: {field.type}")


def _description_word_stats(
    connection: duckdb.DuckDBPyConnection,
    *,
    localized: bool,
) -> tuple[int, int, float | None]:
    values_query = (
        """
        SELECT entry.value AS value
        FROM (
            SELECT unnest(map_entries(localized_descriptions)) AS entry
            FROM features
            WHERE cardinality(localized_descriptions) > 0
        )
        """
        if localized
        else "SELECT description AS value FROM features WHERE description IS NOT NULL"
    )
    row = connection.execute(
        rf"""
        WITH description_values AS ({values_query}),
        word_counts AS (
            SELECT list_count(regexp_extract_all(value, '[^\s\p{{Z}}]+')) AS word_count
            FROM description_values
        )
        SELECT COUNT(*), COALESCE(SUM(word_count), 0), quantile_cont(word_count, 0.5)
        FROM word_counts
        """  # noqa: S608 - values_query is selected from two static statements
    ).fetchone()
    if row is None:
        return 0, 0, None
    return int(row[0]), int(row[1]), float(row[2]) if row[2] is not None else None


@dataclass(frozen=True)
class _ValidatedArtifact:
    parquet: Path
    manifest: Manifest


@dataclass(frozen=True)
class _FeatureSummary:
    rows: int
    unique_osm_objects: int
    osm_types: dict[str, int]
    geometry_types: dict[str, int]
    description_suffixes: dict[str, int]
    name_suffixes: dict[str, int]
    base_description_rows: int
    localized_description_rows: int
    base_description_values: int
    base_description_words_total: int
    base_description_words_median: float | None
    localized_description_values: int
    localized_description_words_total: int
    localized_description_words_median: float | None
    base_name_rows: int
    localized_name_rows: int
    area_min_m2: float | None
    area_p25_m2: float | None
    area_median_m2: float | None
    area_p75_m2: float | None
    area_max_m2: float | None
    data_min_timestamp_utc: str | None
    data_max_timestamp_utc: str | None
    area_total_m2: float = 0.0
    area_mean_m2: float | None = None
    dataset_bbox: tuple[float, float, float, float] | None = None
    geometry_vertices_total: int = 0
    geometry_rings_total: int = 0
    geometry_holes_total: int = 0
    multipolygon_components_total: int = 0
    raw_rows: int | None = None
    raw_successful_text_rows: int | None = None
    all_unique_osm_objects: int | None = None


@dataclass(frozen=True)
class _SpatialSummary:
    rows: int
    area_total_m2: float
    area_mean_m2: float | None
    dataset_bbox: tuple[float, float, float, float] | None
    geometry_vertices_total: int
    geometry_rings_total: int
    geometry_holes_total: int
    multipolygon_components_total: int


@dataclass(frozen=True)
class _ManifestSummary:
    emitted_features: int
    rejections: dict[str, int]
    source_bytes_total: int
    output_bytes_total: int
    files: list[dict[str, Any]]


def _reporting_directories(data_root: Path) -> tuple[Path, Path]:
    data_dir = data_root / "data"
    manifests_dir = data_root / "manifests"
    if not data_dir.is_dir() or not manifests_dir.is_dir():
        raise ReportingError(f"missing data/ or manifests/ under {data_root}")
    return data_dir, manifests_dir


def _matching_parquets(data_dir: Path, manifests_dir: Path) -> list[Path]:
    # pragma: no mutate start - all glob results share data_dir, so Path order equals name order
    parquets = sorted(data_dir.glob("*.parquet"), key=lambda path: path.name)
    # pragma: no mutate end
    parquet_stems = {path.name.removesuffix(".parquet") for path in parquets}
    manifest_stems = {
        path.name.removesuffix(".manifest.json") for path in manifests_dir.glob("*.manifest.json")
    }
    mismatch = parquet_stems.symmetric_difference(manifest_stems)
    if mismatch:
        raise ReportingError(f"artifact/manifest mismatch (missing or extra): {sorted(mismatch)}")
    return parquets


def _validate_artifact(parquet: Path, manifests_dir: Path) -> _ValidatedArtifact:
    stem = parquet.name.removesuffix(".parquet")
    try:
        manifest = read_manifest(_manifest_path_for(parquet.name, manifests_dir.parent))
    except ManifestError as error:
        raise ReportingError(f"cannot read manifest for {stem}: {error}") from error
    actual_output = output_identity_for(parquet)
    if manifest.output != actual_output:
        raise ReportingError(f"stale output identity for {parquet.name}")
    return _ValidatedArtifact(parquet=parquet, manifest=manifest)


def _find_validated_artifacts(data_root: Path) -> tuple[_ValidatedArtifact, ...]:
    data_dir, manifests_dir = _reporting_directories(data_root)
    parquets = _matching_parquets(data_dir, manifests_dir)
    artifacts = tuple(_validate_artifact(parquet, manifests_dir) for parquet in parquets)
    for artifact in artifacts:
        validate_geoparquet(
            artifact.parquet,
            require_successful_text=False,
        )
    return artifacts


def _create_feature_table(connection: duckdb.DuckDBPyConnection) -> None:
    connection.execute(
        """
        CREATE TABLE all_features (
            source_pbf VARCHAR NOT NULL,
            osm_type VARCHAR NOT NULL,
            osm_id BIGINT NOT NULL,
            osm_url VARCHAR NOT NULL,
            version INTEGER,
            changeset BIGINT,
            timestamp TIMESTAMP,
            name VARCHAR,
            localized_names MAP(VARCHAR, VARCHAR) NOT NULL,
            description VARCHAR,
            localized_descriptions MAP(VARCHAR, VARCHAR) NOT NULL,
            tags MAP(VARCHAR, VARCHAR) NOT NULL,
            geometry_type VARCHAR NOT NULL,
            area_m2 DOUBLE NOT NULL,
            bbox_min_x DOUBLE NOT NULL,
            bbox_min_y DOUBLE NOT NULL,
            bbox_max_x DOUBLE NOT NULL,
            bbox_max_y DOUBLE NOT NULL,
            geometry BLOB NOT NULL
        )
        """
    )


def _create_unique_feature_view(connection: duckdb.DuckDBPyConnection) -> None:
    connection.execute(
        "CREATE TEMP VIEW features AS "
        + unique_rows_sql(
            "all_features",
            _FEATURE_COLUMNS,
            key_value_columns_are_maps=True,
            require_successful_text=True,
        )
    )


def _insert_batch(
    connection: duckdb.DuckDBPyConnection,
    batch: pa.RecordBatch,
    _source_name: str,
) -> None:
    localized_names_sql = _map_sql_expression(batch, "localized_names")
    localized_descriptions_sql = _map_sql_expression(batch, "localized_descriptions")
    tags_sql = _map_sql_expression(batch, "tags")
    geometry_sql = canonical_geometry_wkb_sql("geometry")
    connection.register("batch", batch)
    try:
        connection.execute(
            f"""
            INSERT INTO all_features
            SELECT
                source_pbf,
                osm_type,
                osm_id,
                osm_url,
                version,
                changeset,
                timestamp,
                name,
                CASE WHEN {localized_names_sql} IS NULL
                     THEN MAP() ELSE {localized_names_sql} END,
                description,
                CASE WHEN {localized_descriptions_sql} IS NULL
                     THEN MAP() ELSE {localized_descriptions_sql} END,
                CASE WHEN {tags_sql} IS NULL
                     THEN MAP() ELSE {tags_sql} END,
                geometry_type,
                area_m2,
                bbox_min_x,
                bbox_min_y,
                bbox_max_x,
                bbox_max_y,
                {geometry_sql}
            FROM batch
            """,  # noqa: S608 - expressions are internal fixed column names
        )
    finally:
        connection.unregister("batch")


def _ingest_features(
    connection: duckdb.DuckDBPyConnection,
    artifacts: tuple[_ValidatedArtifact, ...],
) -> None:
    """Stream every finalized Parquet batch into the feature table."""
    for artifact in artifacts:
        file_reader = pq.ParquetFile(artifact.parquet)
        for batch in file_reader.iter_batches(
            columns=_FEATURE_COLUMNS,
            batch_size=4096,
        ):
            _insert_batch(connection, batch, artifact.parquet.name)


def _query_int(connection: duckdb.DuckDBPyConnection, query: str) -> int:
    result = connection.execute(query).fetchone()
    return int(result[0] if result else 0)


def _ordered_counts(connection: duckdb.DuckDBPyConnection, query: str) -> dict[str, int]:
    return {str(key): int(value) for key, value in connection.execute(query).fetchall()}


def _collect_feature_summary(connection: duckdb.DuckDBPyConnection) -> _FeatureSummary:
    all_unique_osm_objects = _query_int(
        connection,
        "SELECT COUNT(*) FROM (SELECT DISTINCT osm_type, osm_id FROM all_features)",
    )
    rows = _query_int(connection, "SELECT COUNT(*) FROM features")
    raw_successful_text_rows = _query_int(
        connection,
        "SELECT COUNT(*) FROM all_features WHERE "  # noqa: S608 - internal fixed columns
        + successful_description_text_sql(localized_is_map=True),
    )
    unique_osm_objects = _query_int(
        connection,
        "SELECT COUNT(*) FROM (SELECT DISTINCT osm_type, osm_id FROM features)",
    )
    osm_types = _ordered_counts(
        connection,
        "SELECT osm_type, COUNT(*) FROM features GROUP BY osm_type ORDER BY osm_type",
    )
    geometry_types = _ordered_counts(
        connection,
        "SELECT geometry_type, COUNT(*) FROM features GROUP BY geometry_type "
        "ORDER BY geometry_type",
    )
    description_suffixes = _suffix_counts(connection, "localized_descriptions")
    name_suffixes = _suffix_counts(connection, "localized_names")
    base_description_rows = _query_int(
        connection,
        "SELECT COUNT(*) FROM features WHERE description IS NOT NULL",
    )
    localized_description_rows = _query_int(
        connection,
        "SELECT COUNT(*) FROM features WHERE cardinality(localized_descriptions) > 0",
    )
    base_name_rows = _query_int(
        connection,
        "SELECT COUNT(*) FROM features WHERE name IS NOT NULL",
    )
    localized_name_rows = _query_int(
        connection,
        "SELECT COUNT(*) FROM features WHERE cardinality(localized_names) > 0",
    )
    timestamp_row = connection.execute(
        "SELECT MIN(timestamp), MAX(timestamp) FROM features WHERE timestamp IS NOT NULL"
    ).fetchone()
    min_ts = timestamp_row[0] if timestamp_row else None
    max_ts = timestamp_row[1] if timestamp_row else None
    base_description_values, base_description_words_total, base_description_words_median = (
        _description_word_stats(connection, localized=False)
    )
    (
        localized_description_values,
        localized_description_words_total,
        localized_description_words_median,
    ) = _description_word_stats(connection, localized=True)
    return _FeatureSummary(
        rows=rows,
        unique_osm_objects=unique_osm_objects,
        osm_types=osm_types,
        geometry_types=geometry_types,
        description_suffixes=description_suffixes,
        name_suffixes=name_suffixes,
        base_description_rows=base_description_rows,
        localized_description_rows=localized_description_rows,
        base_description_values=base_description_values,
        base_description_words_total=base_description_words_total,
        base_description_words_median=base_description_words_median,
        localized_description_values=localized_description_values,
        localized_description_words_total=localized_description_words_total,
        localized_description_words_median=localized_description_words_median,
        base_name_rows=base_name_rows,
        localized_name_rows=localized_name_rows,
        area_min_m2=_quantile_or_none(connection, "area_m2", 0.0),
        area_p25_m2=_quantile_or_none(connection, "area_m2", _QUANTILE_PROBABILITIES[0]),
        area_median_m2=_quantile_or_none(connection, "area_m2", _QUANTILE_PROBABILITIES[1]),
        area_p75_m2=_quantile_or_none(connection, "area_m2", _QUANTILE_PROBABILITIES[2]),
        area_max_m2=_quantile_or_none(connection, "area_m2", 1.0),
        data_min_timestamp_utc=min_ts.isoformat() if min_ts else None,
        data_max_timestamp_utc=max_ts.isoformat() if max_ts else None,
        raw_successful_text_rows=raw_successful_text_rows,
        all_unique_osm_objects=all_unique_osm_objects,
    )


def _decode_geometry(
    wkb: bytes,
    expected_type: str,
    *,
    source_name: str,
    row_index: int,
) -> BaseGeometry:
    """Decode and validate one geometry value for reporting."""
    try:
        geometry = from_wkb(wkb)
    except (ValueError, ShapelyError) as error:
        raise ReportingError(
            f"malformed geometry in {source_name} at row {row_index}: {error}"
        ) from error
    if geometry.is_empty:
        raise ReportingError(
            f"invalid geometry in {source_name} at row {row_index}: "
            f"expected {expected_type!r}, got {geometry.geom_type!r}"
        )
    if not geometry.is_valid:
        raise ReportingError(
            f"invalid geometry in {source_name} at row {row_index}: "
            f"expected {expected_type!r}, got {geometry.geom_type!r}"
        )
    if geometry.geom_type != expected_type:
        raise ReportingError(
            f"invalid geometry in {source_name} at row {row_index}: "
            f"expected {expected_type!r}, got {geometry.geom_type!r}"
        )
    return geometry


def _polygon_components(
    geometry: BaseGeometry,
    *,
    source_name: str,
    row_index: int,
) -> tuple[tuple[Polygon, ...], int]:
    """Return polygon components and the MultiPolygon component count."""
    polygons: tuple[Polygon, ...]
    if isinstance(geometry, Polygon):
        return (geometry,), 0
    if isinstance(geometry, MultiPolygon):
        polygons = tuple(geometry.geoms)
        return polygons, len(polygons)
    # pragma: no cover - schema validation prevents this branch
    else:
        raise ReportingError(
            f"unsupported geometry in {source_name} at row {row_index}: {geometry.geom_type!r}"
        )


def _polygon_measurements(polygons: tuple[Polygon, ...]) -> tuple[int, int, int]:
    """Count unique vertices, rings, and holes across polygon components."""
    vertices = 0
    rings = 0
    holes = 0
    for polygon in polygons:
        all_rings = (polygon.exterior, *polygon.interiors)
        rings += len(all_rings)
        holes += len(polygon.interiors)
        vertices += sum(len(ring.coords) - 1 for ring in all_rings)
    return vertices, rings, holes


def _geometry_measurements(
    wkb: bytes,
    expected_type: str,
    *,
    source_name: str,
    row_index: int,
) -> tuple[int, int, int, int]:
    """Return vertex, ring, hole, and MultiPolygon-part totals for one row.

    A ring's closing coordinate is not counted twice as a vertex. Polygon
    components in a MultiPolygon are counted; a simple Polygon contributes
    zero to the MultiPolygon-part total.
    """
    geometry = _decode_geometry(
        wkb,
        expected_type,
        source_name=source_name,
        row_index=row_index,
    )
    polygons, multipolygon_components = _polygon_components(
        geometry,
        source_name=source_name,
        row_index=row_index,
    )
    vertices, rings, holes = _polygon_measurements(polygons)
    return vertices, rings, holes, multipolygon_components


def _validated_area(value: object, *, source_name: str, row_index: int) -> float:
    """Validate and normalize one persisted area value."""
    if isinstance(value, bool) or not isinstance(value, int | float):
        raise ReportingError(f"invalid area in {source_name} at row {row_index}")
    area = float(value)
    if not math.isfinite(area):
        raise ReportingError(f"invalid area in {source_name} at row {row_index}")
    return area


def _validated_geometry_type(value: object, *, source_name: str, row_index: int) -> str:
    """Validate one persisted geometry type value."""
    if not isinstance(value, str):
        raise ReportingError(f"missing geometry type in {source_name} at row {row_index}")
    return value


def _validated_bbox(
    values: tuple[object, ...],
    *,
    source_name: str,
    row_index: int,
) -> tuple[float, float, float, float]:
    """Validate one persisted bounding-box tuple."""
    coordinates = _coerce_bbox_values(values)
    if coordinates is None:
        raise ReportingError(f"invalid bounding box in {source_name} at row {row_index}")
    return coordinates[0], coordinates[1], coordinates[2], coordinates[3]


def _coerce_float_values(values: tuple[object, ...]) -> tuple[float, ...] | None:
    """Convert object values to floats, returning ``None`` on conversion errors."""
    try:
        return tuple(float(cast(Any, value)) for value in values)
    except (TypeError, ValueError):
        return None


def _coerce_bbox_values(values: tuple[object, ...]) -> tuple[float, ...] | None:
    """Convert a bounding-box tuple and reject invalid coordinates."""
    coordinates = _coerce_float_values(values)
    if coordinates is None:
        return None
    if len(coordinates) != 4:
        return None
    if not all(map(math.isfinite, coordinates)):
        return None
    return coordinates


def _summarize_spatial_batch(
    batch: pa.RecordBatch,
    *,
    source_name: str,
    row_offset: int,
) -> _SpatialSummary:
    """Validate and summarize one bounded spatial Arrow batch."""
    areas = batch.column("area_m2").to_pylist()
    geometry_types = batch.column("geometry_type").to_pylist()
    bbox_values = zip(
        batch.column("bbox_min_x").to_pylist(),
        batch.column("bbox_min_y").to_pylist(),
        batch.column("bbox_max_x").to_pylist(),
        batch.column("bbox_max_y").to_pylist(),
        strict=True,
    )
    geometries = batch.column("geometry").to_pylist()
    source_names = (
        batch.column("source_pbf").to_pylist()
        if "source_pbf" in batch.schema.names
        else [source_name] * len(areas)
    )
    area_values: list[float] = []
    bboxes: list[tuple[float, float, float, float]] = []
    vertices_total = 0
    rings_total = 0
    holes_total = 0
    multipolygon_components_total = 0
    for offset, (area, geometry_type, bbox, wkb, row_source) in enumerate(
        zip(areas, geometry_types, bbox_values, geometries, source_names, strict=True)
    ):
        index = row_offset + offset
        source = str(row_source)
        area_values.append(_validated_area(area, source_name=source, row_index=index))
        validated_type = _validated_geometry_type(
            geometry_type,
            source_name=source,
            row_index=index,
        )
        if not isinstance(wkb, bytes):
            raise ReportingError(f"missing geometry in {source} at row {index}")
        vertices, rings, holes, components = _geometry_measurements(
            wkb,
            validated_type,
            source_name=source,
            row_index=index,
        )
        bboxes.append(_validated_bbox(bbox, source_name=source, row_index=index))
        vertices_total += vertices
        rings_total += rings
        holes_total += holes
        multipolygon_components_total += components
    columns = tuple(zip(*bboxes, strict=True))
    min_x = min(columns[0])
    min_y = min(columns[1])
    max_x = max(columns[2])
    max_y = max(columns[3])
    return _SpatialSummary(
        rows=len(area_values),
        area_total_m2=math.fsum(area_values),
        area_mean_m2=None,
        dataset_bbox=(min_x, min_y, max_x, max_y),
        geometry_vertices_total=vertices_total,
        geometry_rings_total=rings_total,
        geometry_holes_total=holes_total,
        multipolygon_components_total=multipolygon_components_total,
    )


def _merge_bboxes(
    current: tuple[float, float, float, float] | None,
    addition: tuple[float, float, float, float] | None,
) -> tuple[float, float, float, float] | None:
    """Merge dataset extents using min/min/max/max semantics."""
    if addition is None:
        return current
    if current is None:
        return addition
    return (
        min(current[0], addition[0]),
        min(current[1], addition[1]),
        max(current[2], addition[2]),
        max(current[3], addition[3]),
    )


def _collect_spatial_summary(artifacts: tuple[_ValidatedArtifact, ...]) -> _SpatialSummary:
    """Stream unique spatial rows and derive dataset-wide geometry facts."""
    if not artifacts:
        return _SpatialSummary(0, 0.0, None, None, 0, 0, 0, 0)
    data_root = artifacts[0].parquet.parent.parent
    # Areas are summed exactly at the end rather than accumulated batch by
    # batch: a running float total depends on the order the batches arrive in,
    # which made stats.json differ in its last bits between identical runs.
    batch_area_totals: list[float] = []
    row_count = 0
    dataset_bbox: tuple[float, float, float, float] | None = None
    vertices_total = 0
    rings_total = 0
    holes_total = 0
    multipolygon_components_total = 0

    row_index = 0
    for batch in iter_unique_parquet_batches(
        data_root,
        columns=_SPATIAL_COLUMNS,
        require_successful_text=True,
    ):
        summary = _summarize_spatial_batch(
            batch,
            source_name="unique.parquet",
            row_offset=row_index,
        )
        dataset_bbox = _merge_bboxes(dataset_bbox, summary.dataset_bbox)
        batch_area_totals.append(summary.area_total_m2)
        row_count += summary.rows
        row_index += summary.rows
        vertices_total += summary.geometry_vertices_total
        rings_total += summary.geometry_rings_total
        holes_total += summary.geometry_holes_total
        multipolygon_components_total += summary.multipolygon_components_total

    area_total = math.fsum(batch_area_totals)
    return _SpatialSummary(
        rows=row_count,
        area_total_m2=area_total,
        area_mean_m2=area_total / row_count if row_count else None,
        dataset_bbox=dataset_bbox,
        geometry_vertices_total=vertices_total,
        geometry_rings_total=rings_total,
        geometry_holes_total=holes_total,
        multipolygon_components_total=multipolygon_components_total,
    )


def _rows_in_parquet(path: Path) -> int:
    metadata = pq.ParquetFile(path).metadata
    return int(metadata.num_rows if metadata else 0)


def _collect_manifest_summary(
    artifacts: tuple[_ValidatedArtifact, ...],
) -> _ManifestSummary:
    rejections: dict[str, int] = {}
    emitted_features = 0
    source_bytes = 0
    output_bytes = 0
    file_records: list[dict[str, Any]] = []
    for artifact in artifacts:
        parquet = artifact.parquet
        manifest = artifact.manifest
        output_size = parquet.stat().st_size
        output_bytes += output_size
        source_bytes += manifest.source.size_bytes
        emitted_features += manifest.counts.emitted_features
        for reason, count in manifest.counts.rejections.items():
            rejections[reason] = rejections.get(reason, 0) + count
        file_records.append(
            {
                "source_pbf": manifest.source.name,
                "parquet": parquet.name,
                "rows": _rows_in_parquet(parquet),
                "source_bytes": manifest.source.size_bytes,
                "output_bytes": output_size,
                "emitted_features": manifest.counts.emitted_features,
                "rejections": dict(sorted(manifest.counts.rejections.items())),
                "source_sha256": manifest.source.sha256,
                "output_sha256": file_sha256(parquet),
            }
        )
    file_records.sort(key=lambda record: record["parquet"])
    return _ManifestSummary(
        emitted_features=emitted_features,
        rejections=dict(sorted(rejections.items())),
        source_bytes_total=source_bytes,
        output_bytes_total=output_bytes,
        files=file_records,
    )


def _raw_row_count(feature_summary: _FeatureSummary) -> int:
    return feature_summary.rows if feature_summary.raw_rows is None else feature_summary.raw_rows


def _globally_unique_count(feature_summary: _FeatureSummary) -> int:
    return (
        feature_summary.unique_osm_objects
        if feature_summary.all_unique_osm_objects is None
        else feature_summary.all_unique_osm_objects
    )


def _text_rejection_counts(manifest_summary: _ManifestSummary) -> dict[str, int]:
    return {reason: manifest_summary.rejections.get(reason, 0) for reason in TEXT_REJECTION_REASONS}


def _overlap_rate(duplicates: int, rows: int) -> float:
    return duplicates / rows if rows else 0.0


def _dataset_bbox_value(feature_summary: _FeatureSummary) -> list[float] | None:
    return list(feature_summary.dataset_bbox) if feature_summary.dataset_bbox is not None else None


def _build_stats_payload(
    feature_summary: _FeatureSummary,
    manifest_summary: _ManifestSummary,
) -> dict[str, Any]:
    raw_rows = _raw_row_count(feature_summary)
    globally_unique = _globally_unique_count(feature_summary)
    successful_text_unique = feature_summary.rows
    regional_successful_text_rows = (
        feature_summary.rows
        if feature_summary.raw_successful_text_rows is None
        else feature_summary.raw_successful_text_rows
    )
    duplicate_rows = raw_rows - globally_unique
    text_rejection_counts = _text_rejection_counts(manifest_summary)
    persisted_text_rejection_rows = raw_rows - regional_successful_text_rows
    manifest_duplicate_rows = manifest_summary.rejections.get("duplicate_osm_object", 0)
    return {
        "stats_schema_version": STATS_SCHEMA_VERSION,
        "schema_version": SCHEMA_VERSION,
        "output_files": len(manifest_summary.files),
        "rows": feature_summary.rows,
        "regional_rows": raw_rows,
        "regional_rows_with_successful_nonempty_text": regional_successful_text_rows,
        "globally_unique_polygons": globally_unique,
        "unique_osm_objects": globally_unique,
        "unique_polygons_with_successful_nonempty_text": successful_text_unique,
        "unique_polygons_with_text": successful_text_unique,
        "regional_overlap_duplicate_rows": duplicate_rows,
        "regional_overlap_duplicate_rate": _overlap_rate(duplicate_rows, raw_rows),
        "emitted_features": manifest_summary.emitted_features,
        "osm_types": feature_summary.osm_types,
        "geometry_types": feature_summary.geometry_types,
        "description_suffixes": feature_summary.description_suffixes,
        "name_suffixes": feature_summary.name_suffixes,
        "base_description_rows": feature_summary.base_description_rows,
        "localized_description_rows": feature_summary.localized_description_rows,
        "base_description_values": feature_summary.base_description_values,
        "base_description_words_total": feature_summary.base_description_words_total,
        "base_description_words_median": feature_summary.base_description_words_median,
        "localized_description_values": feature_summary.localized_description_values,
        "localized_description_words_total": feature_summary.localized_description_words_total,
        "localized_description_words_median": feature_summary.localized_description_words_median,
        "base_name_rows": feature_summary.base_name_rows,
        "localized_name_rows": feature_summary.localized_name_rows,
        "rejections": manifest_summary.rejections,
        "text_rejection_counts": text_rejection_counts,
        "text_rejection_rows": sum(text_rejection_counts.values()),
        "persisted_text_rejection_rows": persisted_text_rejection_rows,
        "deduplicated_rows": duplicate_rows,
        "manifest_duplicate_rows": manifest_duplicate_rows,
        "source_bytes_total": manifest_summary.source_bytes_total,
        "output_bytes_total": manifest_summary.output_bytes_total,
        "area_m2_count": feature_summary.rows,
        "area_m2_population": "unique_polygons_with_successfully_extracted_trimmed_nonempty_text",
        "area_m2_total_m2": feature_summary.area_total_m2,
        "area_m2_mean_m2": feature_summary.area_mean_m2,
        "area_m2_min_m2": feature_summary.area_min_m2,
        "area_m2_p25_m2": feature_summary.area_p25_m2,
        "area_m2_median_m2": feature_summary.area_median_m2,
        "area_m2_p75_m2": feature_summary.area_p75_m2,
        "area_m2_max_m2": feature_summary.area_max_m2,
        "dataset_bbox": _dataset_bbox_value(feature_summary),
        "geometry_vertices_total": feature_summary.geometry_vertices_total,
        "geometry_rings_total": feature_summary.geometry_rings_total,
        "geometry_holes_total": feature_summary.geometry_holes_total,
        "multipolygon_components_total": feature_summary.multipolygon_components_total,
        "data_min_timestamp_utc": feature_summary.data_min_timestamp_utc,
        "data_max_timestamp_utc": feature_summary.data_max_timestamp_utc,
        "files": manifest_summary.files,
    }


def collect_stats(
    data_root: Path,
    *,
    clock: Callable[[], str] = utc_now_iso,
) -> dict[str, Any]:
    """Aggregate factual statistics from validated artifacts and matching manifests."""
    artifacts = _find_validated_artifacts(data_root)

    connection = _new_connection(data_root)
    try:
        _create_feature_table(connection)
        _ingest_features(connection, artifacts)
        _create_unique_feature_view(connection)
        feature_summary = _collect_feature_summary(connection)
        raw_rows = _query_int(connection, "SELECT COUNT(*) FROM all_features")
    finally:
        connection.close()

    spatial_summary = _collect_spatial_summary(artifacts)
    if spatial_summary.rows != feature_summary.rows:
        raise ReportingError(
            f"feature/spatial row count mismatch: {feature_summary.rows} != {spatial_summary.rows}"
        )
    feature_summary = replace(
        feature_summary,
        raw_rows=raw_rows,
        area_total_m2=spatial_summary.area_total_m2,
        area_mean_m2=spatial_summary.area_mean_m2,
        dataset_bbox=spatial_summary.dataset_bbox,
        geometry_vertices_total=spatial_summary.geometry_vertices_total,
        geometry_rings_total=spatial_summary.geometry_rings_total,
        geometry_holes_total=spatial_summary.geometry_holes_total,
        multipolygon_components_total=spatial_summary.multipolygon_components_total,
    )
    return _build_stats_payload(feature_summary, _collect_manifest_summary(artifacts))


__all__ = [
    "STATS_SCHEMA_VERSION",
    "TEXT_REJECTION_REASONS",
    "ReportingError",
    "collect_stats",
    "utc_now_iso",
]
