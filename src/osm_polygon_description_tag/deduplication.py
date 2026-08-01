"""Compatibility exports for :mod:`osm_polygon_description_tag.dataset.deduplication`."""

from osm_polygon_description_tag.dataset.deduplication import (
    DEDUPLICATION_POLICY_SHA256,
    DEDUPLICATION_POLICY_VERSION,
    DUPLICATE_REJECTION_REASON,
    DeduplicationError,
    DeduplicationResult,
    deduplicate_dataset,
    select_canonical_row,
)

__all__ = [
    "DEDUPLICATION_POLICY_SHA256",
    "DEDUPLICATION_POLICY_VERSION",
    "DUPLICATE_REJECTION_REASON",
    "DeduplicationError",
    "DeduplicationResult",
    "deduplicate_dataset",
    "select_canonical_row",
]
