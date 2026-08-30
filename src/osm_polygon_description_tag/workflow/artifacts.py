"""Canonical paths for one discovered source's local artifacts."""

from pathlib import Path

from osm_polygon_description_tag.osm.discovery import Source
from osm_polygon_description_tag.runtime.config import Paths


def source_artifact_paths(paths: Paths, source: Source) -> tuple[Path, Path]:
    """Return the output Parquet and manifest paths for ``source``."""
    stem = source.output_name.removesuffix(".parquet")
    return (
        paths.data_root / "data" / source.output_name,
        paths.data_root / "manifests" / f"{stem}.manifest.json",
    )
