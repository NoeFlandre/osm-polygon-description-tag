"""Compatibility exports for :mod:`osm_polygon_description_tag.dataset.reporting`."""

from osm_polygon_description_tag.dataset.reporting import (
    ReportingError,
    collect_stats,
    generate_dataset_docs,
    utc_now_iso,
)

__all__ = ["ReportingError", "collect_stats", "generate_dataset_docs", "utc_now_iso"]
