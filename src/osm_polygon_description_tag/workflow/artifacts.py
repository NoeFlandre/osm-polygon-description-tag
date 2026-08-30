"""Canonical paths for one discovered source's local artifacts."""

from pathlib import Path

from osm_polygon_description_tag.dataset.manifest import _manifest_path_for
from osm_polygon_description_tag.osm.discovery import Source
from osm_polygon_description_tag.runtime.config import Paths


def source_artifact_paths(paths: Paths, source: Source) -> tuple[Path, Path]:
    """Return the output Parquet and manifest paths for ``source``."""
    output_path = paths.data_root / "data" / source.output_name
    return (
        output_path,
        _manifest_path_for(source.output_name, paths.data_root),
    )
