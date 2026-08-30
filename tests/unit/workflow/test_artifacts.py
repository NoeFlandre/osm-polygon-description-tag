from pathlib import Path

from osm_polygon_description_tag.osm.discovery import Source
from osm_polygon_description_tag.runtime.config import Paths


def test_source_artifact_paths_use_output_stem_for_manifest() -> None:
    from osm_polygon_description_tag.workflow.artifacts import source_artifact_paths

    paths = Paths(source_root=Path("raw"), data_root=Path("generated"))
    source = Source(
        path=Path("raw/region.osm.pbf"),
        name="region.osm.pbf",
        output_name="region.parquet",
        size_bytes=0,
        mtime_ns=0,
    )

    assert source_artifact_paths(paths, source) == (
        Path("generated/data/region.parquet"),
        Path("generated/manifests/region.manifest.json"),
    )
