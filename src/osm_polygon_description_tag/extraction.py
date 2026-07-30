"""Compatibility exports for :mod:`osm_polygon_description_tag.osm.extraction`."""

from osm_polygon_description_tag.osm.extraction import (
    STDERR_CAP_BYTES,
    ExportRecord,
    OsmiumExportError,
    export_command,
    iter_records,
    osmium_version,
    parse_copy_record,
    stream_export,
)

__all__ = [
    "STDERR_CAP_BYTES",
    "ExportRecord",
    "OsmiumExportError",
    "export_command",
    "iter_records",
    "osmium_version",
    "parse_copy_record",
    "stream_export",
]
