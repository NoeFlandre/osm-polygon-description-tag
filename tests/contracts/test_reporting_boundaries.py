"""Canonical module boundaries for statistics and documentation generation."""

from osm_polygon_description_tag.dataset import docs, stats


def test_stats_exposes_its_schema_version_from_the_stats_module() -> None:
    """The stats module owns the version of its machine-readable output."""
    assert stats.STATS_SCHEMA_VERSION == 6


def test_docs_generator_is_implemented_by_the_docs_module() -> None:
    assert docs.generate_dataset_docs.__module__ == docs.__name__
