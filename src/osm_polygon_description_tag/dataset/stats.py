"""Artifact-derived dataset statistics.

Statistics read only validated Parquet files with matching manifests and are
computed from the finalized artifacts. Handwritten numeric claims are never
introduced here.

This module composes the stats.json payload from three helpers: stats_manifest
validates the artifacts and summarizes their manifests, stats_features aggregates
feature rows in DuckDB, and stats_geometry measures geometry and extents.

Bounded memory: aggregate statistics are computed by streaming each Parquet
file into an in-memory DuckDB instance using per-batch Arrow ingestion. Exact
area quantiles use ``quantile_cont`` on the DuckDB-backed ``area_m2`` column;
the additional area, extent, and geometry-complexity pass reads one batch at a
time, so it does not retain the dataset's WKB in Python.

"""

from __future__ import annotations

from dataclasses import replace
from pathlib import Path
from typing import Any

from osm_polygon_description_tag.dataset.duckdb_runtime import (
    open_data_connection as _new_connection,
)
from osm_polygon_description_tag.dataset.schema import SCHEMA_VERSION
from osm_polygon_description_tag.dataset.stats_features import (
    FeatureSummary,
    collect_feature_summary,
    create_feature_table,
    create_unique_feature_view,
    ingest_features,
    query_int,
)
from osm_polygon_description_tag.dataset.stats_geometry import collect_spatial_summary
from osm_polygon_description_tag.dataset.stats_manifest import (
    ManifestSummary,
    ReportingError,
    collect_manifest_summary,
    find_validated_artifacts,
)

STATS_SCHEMA_VERSION = 9

TEXT_REJECTION_REASONS = (
    "no_description",
    "missing_description",
    "no_nonempty_description",
    "blank_description",
    "malformed_description",
    "failed_description_extraction",
)


def _raw_row_count(feature_summary: FeatureSummary) -> int:
    return feature_summary.rows if feature_summary.raw_rows is None else feature_summary.raw_rows


def _globally_unique_count(feature_summary: FeatureSummary) -> int:
    return (
        feature_summary.unique_osm_objects
        if feature_summary.all_unique_osm_objects is None
        else feature_summary.all_unique_osm_objects
    )


def _text_rejection_counts(manifest_summary: ManifestSummary) -> dict[str, int]:
    return {reason: manifest_summary.rejections.get(reason, 0) for reason in TEXT_REJECTION_REASONS}


def _overlap_rate(duplicates: int, rows: int) -> float:
    return duplicates / rows if rows else 0.0


def _dataset_bbox_value(feature_summary: FeatureSummary) -> list[float] | None:
    return list(feature_summary.dataset_bbox) if feature_summary.dataset_bbox is not None else None


def _build_stats_payload(
    feature_summary: FeatureSummary,
    manifest_summary: ManifestSummary,
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


def collect_stats(data_root: Path) -> dict[str, Any]:
    """Aggregate factual statistics from validated artifacts and matching manifests."""
    artifacts = find_validated_artifacts(data_root)

    connection = _new_connection(data_root)
    try:
        create_feature_table(connection)
        ingest_features(connection, artifacts)
        create_unique_feature_view(connection)
        feature_summary = collect_feature_summary(connection)
        # SQL keywords and DuckDB identifiers are both case-insensitive, so no
        # input can tell a re-cased spelling of this query apart from this one.
        raw_rows = query_int(connection, "SELECT COUNT(*) FROM all_features")  # pragma: no mutate
    finally:
        connection.close()

    spatial_summary = collect_spatial_summary(artifacts)
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
    return _build_stats_payload(feature_summary, collect_manifest_summary(artifacts))


__all__ = [
    "STATS_SCHEMA_VERSION",
    "TEXT_REJECTION_REASONS",
    "ReportingError",
    "collect_stats",
]
