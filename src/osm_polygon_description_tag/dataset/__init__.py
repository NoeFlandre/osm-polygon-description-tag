"""Canonical dataset artifact APIs."""

from importlib import import_module

__all__ = [
    "DEDUPLICATION_POLICY_SHA256",
    "DEDUPLICATION_POLICY_VERSION",
    "DUPLICATE_REJECTION_REASON",
    "GEOPARQUET_VERSION",
    "MANIFEST_SCHEMA_VERSION",
    "SCHEMA",
    "SCHEMA_VERSION",
    "TRANSFORM_ALGORITHM_VERSION",
    "DeduplicationError",
    "DeduplicationResult",
    "Manifest",
    "ManifestError",
    "MigrationError",
    "OutputIdentity",
    "RejectedFeature",
    "ReportingError",
    "RunCounts",
    "SourceIdentity",
    "StorageError",
    "TextMigrationError",
    "collect_stats",
    "current_area_policy_sha256",
    "current_code_revision",
    "current_dependency_versions",
    "current_output_algorithm_revision",
    "deduplicate_dataset",
    "descriptions_from_tags",
    "file_sha256",
    "generate_dataset_docs",
    "geo_metadata",
    "geodesic_area_m2",
    "is_resumable",
    "migrate_dataset_schema",
    "migrate_dataset_text",
    "names_from_tags",
    "output_identity_for",
    "read_manifest",
    "select_canonical_row",
    "source_identity_for",
    "transform_record",
    "validate_finalized_artifacts",
    "validate_finalized_artifacts_strict",
    "validate_geoparquet",
    "write_geoparquet",
    "write_manifest",
]

_LAZY_EXPORTS = {
    **{
        name: ("osm_polygon_description_tag.dataset.deduplication", name)
        for name in (
            "DEDUPLICATION_POLICY_SHA256",
            "DEDUPLICATION_POLICY_VERSION",
            "DUPLICATE_REJECTION_REASON",
            "DeduplicationError",
            "DeduplicationResult",
            "deduplicate_dataset",
            "select_canonical_row",
        )
    },
    **{
        name: ("osm_polygon_description_tag.dataset.manifest", name)
        for name in (
            "MANIFEST_SCHEMA_VERSION",
            "TRANSFORM_ALGORITHM_VERSION",
            "Manifest",
            "ManifestError",
            "OutputIdentity",
            "RunCounts",
            "SourceIdentity",
            "current_area_policy_sha256",
            "current_code_revision",
            "current_dependency_versions",
            "current_output_algorithm_revision",
            "file_sha256",
            "is_resumable",
            "output_identity_for",
            "read_manifest",
            "source_identity_for",
            "write_manifest",
        )
    },
    "MigrationError": ("osm_polygon_description_tag.dataset.migration", "MigrationError"),
    "migrate_dataset_schema": (
        "osm_polygon_description_tag.dataset.migration",
        "migrate_dataset_schema",
    ),
    "ReportingError": ("osm_polygon_description_tag.dataset.stats", "ReportingError"),
    "collect_stats": ("osm_polygon_description_tag.dataset.stats", "collect_stats"),
    "generate_dataset_docs": ("osm_polygon_description_tag.dataset.docs", "generate_dataset_docs"),
    **{
        name: ("osm_polygon_description_tag.dataset.schema", name)
        for name in ("GEOPARQUET_VERSION", "SCHEMA", "SCHEMA_VERSION", "geo_metadata")
    },
    **{
        name: ("osm_polygon_description_tag.dataset.storage", name)
        for name in (
            "StorageError",
            "validate_finalized_artifacts",
            "validate_finalized_artifacts_strict",
            "validate_geoparquet",
            "write_geoparquet",
        )
    },
    "TextMigrationError": (
        "osm_polygon_description_tag.dataset.text_migration",
        "TextMigrationError",
    ),
    "migrate_dataset_text": (
        "osm_polygon_description_tag.dataset.text_migration",
        "migrate_dataset_text",
    ),
    **{
        name: ("osm_polygon_description_tag.dataset.transform", name)
        for name in (
            "RejectedFeature",
            "descriptions_from_tags",
            "geodesic_area_m2",
            "names_from_tags",
            "transform_record",
        )
    },
}


def __getattr__(name: str) -> object:
    """Load one dataset subsystem only when a caller actually requests it.

    Lightweight language/checkpoint commands should not initialize optional
    Arrow, geometry, plotting, or model stacks during process startup.
    """
    target = _LAZY_EXPORTS.get(name)
    if target is None:
        raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
    module_name, attribute_name = target
    value = getattr(import_module(module_name), attribute_name)
    globals()[name] = value
    return value
