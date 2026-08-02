"""Compatibility exports for :mod:`osm_polygon_description_tag.dataset.storage`."""

from osm_polygon_description_tag.dataset.storage import (
    StorageError,
    validate_finalized_artifacts,
    validate_finalized_artifacts_strict,
    validate_geoparquet,
    write_geoparquet,
)

__all__ = [
    "StorageError",
    "validate_finalized_artifacts",
    "validate_finalized_artifacts_strict",
    "validate_geoparquet",
    "write_geoparquet",
]
