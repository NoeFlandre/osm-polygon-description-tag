"""Frozen Arrow and GeoParquet 1.1 schema contract.

The geometry column is OGC:CRS84 longitude/latitude. Omitting ``crs`` in the
GeoParquet metadata deliberately signals CRS84; no claim is made about ring
orientation beyond what geometry normalization (see transform) provides.

The schema is versioned. The public key/value fields use a list of structs
instead of Arrow's ``map`` type because Hugging Face Datasets cannot infer
Arrow maps. Each list is sorted by key and preserves the complete original
content while remaining viewable on the Hub.
"""

from collections.abc import Mapping, Sequence

import pyarrow as pa

SCHEMA_VERSION = 3
GEOPARQUET_VERSION = "1.1.0"

KEY_VALUE_STRUCT = pa.struct(
    [
        pa.field("key", pa.string(), nullable=False),
        pa.field("value", pa.string()),
    ]
)
KEY_VALUE_LIST = pa.list_(KEY_VALUE_STRUCT)

KEY_VALUE_COLUMNS = ("localized_names", "localized_descriptions", "tags")


def mapping_to_pairs(value: Mapping[str, str] | Sequence[object] | object) -> list[dict[str, str]]:
    """Encode a string mapping as deterministic key/value records.

    The transform layer continues to expose ordinary dictionaries. This
    boundary representation is only for Arrow storage and the Hub viewer.
    """
    if value is None:
        return []
    if isinstance(value, Mapping):
        items = value.items()
    elif isinstance(value, Sequence) and not isinstance(value, str | bytes):
        converted: dict[str, str] = {}
        for item in value:
            if isinstance(item, Mapping):
                key, mapped = item.get("key"), item.get("value")
            elif isinstance(item, Sequence) and len(item) == 2:
                key, mapped = item
            else:
                raise TypeError(f"invalid key/value record: {item!r}")
            if key is None or mapped is None:
                continue
            converted[str(key)] = str(mapped)
        items = converted.items()
    else:
        raise TypeError(f"expected a string mapping, got {type(value).__name__}")
    return [{"key": str(key), "value": str(item)} for key, item in sorted(items)]


SCHEMA = pa.schema(
    [
        pa.field("source_pbf", pa.string(), nullable=False),
        pa.field("osm_type", pa.string(), nullable=False),
        pa.field("osm_id", pa.int64(), nullable=False),
        pa.field("osm_url", pa.string(), nullable=False),
        pa.field("version", pa.int32()),
        pa.field("changeset", pa.int64()),
        pa.field("timestamp", pa.timestamp("ms", tz="UTC")),
        pa.field("name", pa.string()),
        pa.field("localized_names", KEY_VALUE_LIST, nullable=False),
        pa.field("description", pa.string()),
        pa.field("localized_descriptions", KEY_VALUE_LIST, nullable=False),
        pa.field("tags", KEY_VALUE_LIST, nullable=False),
        pa.field("geometry_type", pa.string(), nullable=False),
        pa.field("area_m2", pa.float64(), nullable=False),
        pa.field("bbox_min_x", pa.float64(), nullable=False),
        pa.field("bbox_min_y", pa.float64(), nullable=False),
        pa.field("bbox_max_x", pa.float64(), nullable=False),
        pa.field("bbox_max_y", pa.float64(), nullable=False),
        pa.field("geometry", pa.binary(), nullable=False),
    ]
)


def geo_metadata(geometry_types: list[str], bbox: list[float]) -> dict[str, object]:
    """Build the GeoParquet 1.1 ``geo`` metadata block for the ``geometry`` column.

    ``bbox`` is omitted when empty (e.g. an empty file) since there is no extent.
    """
    column: dict[str, object] = {
        "encoding": "WKB",
        "geometry_types": sorted(set(geometry_types)),
        "covering": {
            "bbox": {
                "xmin": ["bbox_min_x"],
                "ymin": ["bbox_min_y"],
                "xmax": ["bbox_max_x"],
                "ymax": ["bbox_max_y"],
            }
        },
    }
    if bbox:
        column["bbox"] = bbox
    return {
        "version": "1.1.0",
        "primary_column": "geometry",
        "columns": {"geometry": column},
    }


__all__ = ["GEOPARQUET_VERSION", "SCHEMA", "SCHEMA_VERSION", "geo_metadata"]
