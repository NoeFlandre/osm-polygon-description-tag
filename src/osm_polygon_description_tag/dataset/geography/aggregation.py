"""Pure aggregation for the H3 density map.

The aggregator only reads Parquet inputs, computes the per-cell count,
and returns a deterministic mapping. It performs no rendering or
external I/O.
"""

from __future__ import annotations

from pathlib import Path

from osm_polygon_description_tag.dataset.geography.h3_policy import DEFAULT_H3_RESOLUTION
from osm_polygon_description_tag.dataset.geography.parquet_inputs import collect_h3_counts


def aggregate_h3_density(
    data_root: Path,
    *,
    h3_resolution: int | None = None,
) -> dict[str, int]:
    """Aggregate H3 cell counts over the complete validated local dataset.

    Every row in every Parquet under ``data/`` contributes exactly once
    to the count, preserving regional overlap semantics: the same OSM
    object appearing in two regional extracts is counted as two dataset
    rows, matching the published dataset's row-count contract.

    The ``h3_resolution`` argument uses an explicit ``None`` check:
    passing ``h3_resolution=0`` selects resolution 0 (valid), while
    passing ``h3_resolution=None`` (the default) selects the package
    default :data:`DEFAULT_H3_RESOLUTION`.
    """
    if h3_resolution is None:
        resolution: int = DEFAULT_H3_RESOLUTION
    else:
        resolution = h3_resolution
    return collect_h3_counts(data_root, h3_resolution=resolution)


__all__ = ["aggregate_h3_density"]
