"""Public compatibility facade for dataset statistics and documentation."""

from osm_polygon_description_tag.dataset.docs import generate_dataset_docs
from osm_polygon_description_tag.dataset.stats import ReportingError, collect_stats

__all__ = ["ReportingError", "collect_stats", "generate_dataset_docs"]
