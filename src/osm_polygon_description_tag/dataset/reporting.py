"""Backward-compatible facade for dataset statistics and documentation.

New code should import statistics from :mod:`dataset.stats` and card/media
generation from :mod:`dataset.docs`. This facade preserves the historical
public and test-facing imports while keeping those implementations separated.
"""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path
from typing import Any

from osm_polygon_description_tag.dataset import docs as _docs
from osm_polygon_description_tag.dataset import stats as _stats
from osm_polygon_description_tag.dataset.docs import (
    _area_histogram_input_sha256,
    _h3_map_input_sha256,
    _render_h3_map_block,
    _render_stats_block,
    _write_area_histogram_png,
    _write_h3_map_png,
)
from osm_polygon_description_tag.dataset.geography import (
    aggregate_area_histogram,
    aggregate_h3_density,
    render_area_histogram,
)
from osm_polygon_description_tag.dataset.geography.rendering import render_density_map
from osm_polygon_description_tag.dataset.stats import (
    ReportingError,
    collect_stats,
    utc_now_iso,
)

_new_connection = _stats._new_connection
_safe_map = _stats._safe_map


def _write_if_changed(path: Path, text: str) -> bool:
    """Compatibility alias for the deterministic text writer."""
    return _docs._base_write_if_changed(path, text)


def generate_dataset_docs(
    data_root: Path,
    template_path: Path,
    *,
    clock: Callable[[], str] = utc_now_iso,
) -> dict[str, Any]:
    """Generate stats, README, and derived media through the docs module."""
    # Preserve historical monkeypatch seams used by callers and tests.
    _docs._render_h3_map_block = _render_h3_map_block
    _docs._write_h3_map_png = _write_h3_map_png
    _docs._write_area_histogram_png = _write_area_histogram_png
    _docs._render_stats_block = _render_stats_block
    _docs._area_histogram_input_sha256 = _area_histogram_input_sha256
    _docs._h3_map_input_sha256 = _h3_map_input_sha256
    _docs.aggregate_area_histogram = aggregate_area_histogram
    _docs.aggregate_h3_density = aggregate_h3_density
    _docs.render_area_histogram = render_area_histogram
    _docs.render_density_map = render_density_map
    _docs.collect_stats = collect_stats
    return _docs.generate_dataset_docs(data_root, template_path, clock=clock)


__all__ = [
    "ReportingError",
    "collect_stats",
    "generate_dataset_docs",
    "utc_now_iso",
]
