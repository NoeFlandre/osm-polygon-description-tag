"""Pure feature-to-record transformation for described polygon exports.

This module has no filesystem or subprocess responsibilities. It selects
matching description tags, preserves every original OSM tag, converts geometry
to WKB, computes bounding boxes and geodesic area, and yields one typed record.
"""

from dataclasses import dataclass
from datetime import UTC, datetime

from pyproj import Geod
from shapely import from_wkb, to_wkb
from shapely.errors import ShapelyError
from shapely.geometry.base import BaseGeometry
from shapely.ops import orient

from osm_polygon_description_tag.osm.extraction import ExportRecord

GEOD = Geod(ellps="WGS84")

_OSM_TYPES = {"way", "relation"}


@dataclass(frozen=True)
class RejectedFeature(Exception):
    """A feature-level rejection carrying a stable reason code."""

    reason: str


def descriptions_from_tags(
    tags: dict[str, str],
) -> tuple[str | None, dict[str, str]]:
    """Return ``(base, localized)`` description values.

    ``base`` is the exact ``description`` value, or ``None`` when missing or
    whitespace-only. ``localized`` maps every exact ``description:<suffix>``
    key (excluding the empty suffix) with a non-empty value to its suffix.
    Suffixes are preserved verbatim and are never validated as language codes.
    """
    base = tags.get("description")
    if base is not None and not base.strip():
        base = None
    localized = {
        key.removeprefix("description:"): value
        for key, value in sorted(tags.items())
        if key.startswith("description:") and key != "description:" and value.strip()
    }
    return base, localized


def names_from_tags(tags: dict[str, str]) -> tuple[str | None, dict[str, str]]:
    """Return ``(base, localized)`` name values.

    ``base`` is the exact ``name`` value, or ``None`` when missing or
    whitespace-only. ``localized`` maps every exact ``name:<suffix>`` key
    (excluding the empty suffix) with a non-empty value to its suffix.
    Suffixes are preserved verbatim and are never validated as language codes.
    """
    base = tags.get("name")
    if base is not None and not base.strip():
        base = None
    localized = {
        key.removeprefix("name:"): value
        for key, value in sorted(tags.items())
        if key.startswith("name:") and key != "name:" and value.strip()
    }
    return base, localized


def geodesic_area_m2(geometry: BaseGeometry) -> float:
    """Positive WGS84 geodesic area in square metres.

    Geometry is oriented (exterior counter-clockwise, holes clockwise) first so
    that holes and every multipolygon component contribute correctly regardless
    of the source ring orientation.
    """
    area, _perimeter = GEOD.geometry_area_perimeter(orient(geometry))
    return abs(float(area))


def _optional_timestamp(value: str | None) -> datetime | None:
    if value is None or not value.strip():
        return None
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    return parsed.astimezone(UTC)


def transform_record(record: ExportRecord, source_pbf: str) -> dict[str, object]:
    """Transform one :class:`ExportRecord` into a typed schema record.

    Raises :class:`RejectedFeature` with a stable reason code for any feature
    that does not satisfy the inclusion contract.
    """
    if record.osm_type not in _OSM_TYPES:
        raise RejectedFeature("unsupported_osm_type")
    if record.osm_id <= 0:
        raise RejectedFeature("invalid_osm_id")

    base, localized = descriptions_from_tags(record.tags)
    if base is None and not localized:
        raise RejectedFeature("no_nonempty_description")

    name, localized_names = names_from_tags(record.tags)

    try:
        geometry = from_wkb(bytes.fromhex(record.geometry_ewkb_hex))
    except (ValueError, ShapelyError) as error:
        raise RejectedFeature("invalid_geometry") from error

    if geometry.geom_type not in {"Polygon", "MultiPolygon"}:
        raise RejectedFeature("non_polygon_geometry")
    if geometry.is_empty or not geometry.is_valid:
        raise RejectedFeature("invalid_geometry")

    oriented = orient(geometry)
    area = geodesic_area_m2(oriented)
    if not area > 0:
        raise RejectedFeature("nonpositive_area")

    min_x, min_y, max_x, max_y = oriented.bounds
    osm_type = record.osm_type
    osm_id = record.osm_id
    return {
        "source_pbf": source_pbf,
        "osm_type": osm_type,
        "osm_id": osm_id,
        "osm_url": f"https://www.openstreetmap.org/{osm_type}/{osm_id}",
        "version": record.version,
        "changeset": record.changeset,
        "timestamp": _optional_timestamp(record.timestamp),
        "name": name,
        "localized_names": localized_names,
        "description": base,
        "localized_descriptions": localized,
        "tags": record.tags,
        "geometry_type": oriented.geom_type,
        "area_m2": area,
        "bbox_min_x": min_x,
        "bbox_min_y": min_y,
        "bbox_max_x": max_x,
        "bbox_max_y": max_y,
        "geometry": to_wkb(oriented, output_dimension=2),
    }


__all__ = [
    "GEOD",
    "RejectedFeature",
    "descriptions_from_tags",
    "geodesic_area_m2",
    "names_from_tags",
    "transform_record",
]
