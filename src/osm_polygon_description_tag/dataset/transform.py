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
    return _tag_values(tags, "description")


def names_from_tags(tags: dict[str, str]) -> tuple[str | None, dict[str, str]]:
    """Return ``(base, localized)`` name values.

    ``base`` is the exact ``name`` value, or ``None`` when missing or
    whitespace-only. ``localized`` maps every exact ``name:<suffix>`` key
    (excluding the empty suffix) with a non-empty value to its suffix.
    Suffixes are preserved verbatim and are never validated as language codes.
    """
    return _tag_values(tags, "name")


def _tag_values(tags: dict[str, str], prefix: str) -> tuple[str | None, dict[str, str]]:
    return _clean_base_value(tags.get(prefix)), _localized_values(tags, prefix)


def _clean_base_value(value: str | None) -> str | None:
    return value if value is None or value.strip() else None


def _localized_values(tags: dict[str, str], prefix: str) -> dict[str, str]:
    marker = f"{prefix}:"
    return {
        key.removeprefix(marker): value
        for key, value in sorted(tags.items())
        if key.startswith(marker) and key != marker and value.strip()
    }


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
    parsed = datetime.fromisoformat(value)
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    return parsed.astimezone(UTC)


def _validate_record_identity(record: ExportRecord) -> None:
    if record.osm_type not in _OSM_TYPES:
        raise RejectedFeature("unsupported_osm_type")
    if record.osm_id <= 0:
        raise RejectedFeature("invalid_osm_id")


def _decode_polygon(record: ExportRecord) -> BaseGeometry:
    try:
        geometry = from_wkb(bytes.fromhex(record.geometry_ewkb_hex))
    except (ValueError, ShapelyError) as error:
        raise RejectedFeature("invalid_geometry") from error
    if geometry.geom_type not in {"Polygon", "MultiPolygon"}:
        raise RejectedFeature("non_polygon_geometry")
    if geometry.is_empty or not geometry.is_valid:
        raise RejectedFeature("invalid_geometry")
    return geometry


def _validate_positive_area(geometry: BaseGeometry) -> float:
    area = geodesic_area_m2(geometry)
    if not area > 0:
        raise RejectedFeature("nonpositive_area")
    return area


def _record_payload(
    record: ExportRecord,
    source_pbf: str,
    geometry: BaseGeometry,
    area: float,
    base: str | None,
    localized: dict[str, str],
    name: str | None,
    localized_names: dict[str, str],
) -> dict[str, object]:
    min_x, min_y, max_x, max_y = geometry.bounds
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
        "geometry_type": geometry.geom_type,
        "area_m2": area,
        "bbox_min_x": min_x,
        "bbox_min_y": min_y,
        "bbox_max_x": max_x,
        "bbox_max_y": max_y,
        "geometry": to_wkb(geometry, output_dimension=2),
    }


def transform_record(record: ExportRecord, source_pbf: str) -> dict[str, object]:
    """Transform one :class:`ExportRecord` into a typed schema record.

    Raises :class:`RejectedFeature` with a stable reason code for any feature
    that does not satisfy the inclusion contract.
    """
    _validate_record_identity(record)
    base, localized = descriptions_from_tags(record.tags)
    if base is None and not localized:
        raise RejectedFeature("no_nonempty_description")

    name, localized_names = names_from_tags(record.tags)
    geometry = _decode_polygon(record)
    oriented = orient(geometry)
    area = _validate_positive_area(oriented)
    return _record_payload(
        record,
        source_pbf,
        oriented,
        area,
        base,
        localized,
        name,
        localized_names,
    )


__all__ = [
    "GEOD",
    "RejectedFeature",
    "descriptions_from_tags",
    "geodesic_area_m2",
    "names_from_tags",
    "transform_record",
]
