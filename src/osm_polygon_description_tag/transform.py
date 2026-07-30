"""Compatibility exports for :mod:`osm_polygon_description_tag.dataset.transform`."""

from osm_polygon_description_tag.dataset.transform import (
    GEOD,
    RejectedFeature,
    descriptions_from_tags,
    geodesic_area_m2,
    names_from_tags,
    transform_record,
)

__all__ = [
    "GEOD",
    "RejectedFeature",
    "descriptions_from_tags",
    "geodesic_area_m2",
    "names_from_tags",
    "transform_record",
]
