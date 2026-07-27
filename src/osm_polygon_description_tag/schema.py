"""Frozen Arrow and GeoParquet 1.1 schema contract.

The geometry column is OGC:CRS84 longitude/latitude. Omitting ``crs`` in the
GeoParquet metadata deliberately signals CRS84; no claim is made about ring
orientation beyond what geometry normalization (see transform) provides.
"""

import pyarrow as pa

SCHEMA_VERSION = 1
GEOPARQUET_VERSION = "1.1.0"

SCHEMA = pa.schema(
    [
        pa.field("source_pbf", pa.string(), nullable=False),
        pa.field("osm_type", pa.string(), nullable=False),
        pa.field("osm_id", pa.int64(), nullable=False),
        pa.field("osm_url", pa.string(), nullable=False),
        pa.field("version", pa.int32()),
        pa.field("changeset", pa.int64()),
        pa.field("timestamp", pa.timestamp("ms", tz="UTC")),
        pa.field("description", pa.string()),
        pa.field("localized_descriptions", pa.map_(pa.string(), pa.string()), nullable=False),
        pa.field("tags", pa.map_(pa.string(), pa.string()), nullable=False),
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
