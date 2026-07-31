"""Public geography subpackage for the description-tag dataset.

This subpackage produces the deterministic H3 hexagon density map of the
description-tagged polygons and integrates it as a dataset-card artifact and
a publication asset. The map counts every dataset row exactly once
(preserving regional overlap semantics), uses H3 resolution 3, and uses a
logarithmic colour scale so sparse and dense areas remain visible.

It also produces the deterministic area distribution histogram that replaces
the per-stat table on the dataset card. The histogram buckets every row's
``area_m2`` into fixed logarithmic bins so it stays small, readable, and
byte-identical across runs.

Public orchestration helpers are re-exported below for callers that want a
single import path.
"""

from osm_polygon_description_tag.dataset.geography.aggregation import (
    aggregate_h3_density,
)
from osm_polygon_description_tag.dataset.geography.area_histogram import (
    AREA_BUCKET_COUNT,
    AREA_BUCKET_EDGES,
    AREA_BUCKET_LABELS,
    AREA_HISTOGRAM_RENDER_VERSION,
    aggregate_area_histogram,
    area_histogram_input_sha256,
)
from osm_polygon_description_tag.dataset.geography.area_rendering import (
    AreaHistogramResult,
    render_area_histogram,
)
from osm_polygon_description_tag.dataset.geography.card import (
    H3_MAP_ASSET_RELATIVE_PATH,
    H3_MAP_END_MARKER,
    H3_MAP_START_MARKER,
    H3_MAP_TITLE,
    install_map_block,
    render_map_block,
)
from osm_polygon_description_tag.dataset.geography.h3_policy import (
    DEFAULT_H3_RESOLUTION,
    H3PolicyError,
    assign_h3_cell,
    cell_rings,
    coordinate_to_h3,
    split_antimeridian,
    validate_coordinate,
)
from osm_polygon_description_tag.dataset.geography.parquet_inputs import (
    PARQUET_INPUT_COLUMNS,
    collect_h3_counts,
    iter_centroids,
)
from osm_polygon_description_tag.dataset.geography.rendering import (
    RenderResult,
    render_density_map,
)

__all__ = [
    "AREA_BUCKET_COUNT",
    "AREA_BUCKET_EDGES",
    "AREA_BUCKET_LABELS",
    "AREA_HISTOGRAM_RENDER_VERSION",
    "DEFAULT_H3_RESOLUTION",
    "H3_MAP_ASSET_RELATIVE_PATH",
    "H3_MAP_END_MARKER",
    "H3_MAP_START_MARKER",
    "H3_MAP_TITLE",
    "PARQUET_INPUT_COLUMNS",
    "AreaHistogramResult",
    "H3PolicyError",
    "RenderResult",
    "aggregate_area_histogram",
    "aggregate_h3_density",
    "area_histogram_input_sha256",
    "assign_h3_cell",
    "cell_rings",
    "collect_h3_counts",
    "coordinate_to_h3",
    "install_map_block",
    "iter_centroids",
    "render_area_histogram",
    "render_density_map",
    "render_map_block",
    "split_antimeridian",
    "validate_coordinate",
]
