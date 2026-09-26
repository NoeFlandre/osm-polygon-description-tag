"""Compatibility re-export of :mod:`osm_polygon_description_tag.runtime.atomic`."""

from osm_polygon_description_tag.runtime.atomic import (
    atomic_write_bytes,
    atomic_write_json,
    atomic_write_via,
)
from osm_polygon_description_tag.runtime.serialization import canonical_json_bytes

__all__ = [
    "atomic_write_bytes",
    "atomic_write_json",
    "atomic_write_via",
    "canonical_json_bytes",
]
