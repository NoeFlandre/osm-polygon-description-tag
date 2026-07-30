from pathlib import Path

import pytest

from osm_polygon_description_tag.runtime.config import (
    DEFAULT_DATA_ROOT,
    DEFAULT_SOURCE_ROOT,
    Paths,
    UnsafePathError,
)


def test_paths_use_approved_defaults() -> None:
    paths = Paths.defaults()
    assert paths.source_root == DEFAULT_SOURCE_ROOT
    assert paths.data_root == DEFAULT_DATA_ROOT
    assert paths.source_root == Path("/Volumes/Seagate M3/projects/osm-polygon-wikidata-only/raw")
    assert paths.data_root == Path("/Volumes/Seagate M3/projects/osm-polygon-description-tag")


def test_output_cannot_be_inside_source(tmp_path: Path) -> None:
    source = tmp_path / "raw"
    source.mkdir()
    with pytest.raises(UnsafePathError, match="inside immutable source"):
        Paths(source_root=source, data_root=source / "output").validate()


def test_source_inside_data_root_is_also_rejected(tmp_path: Path) -> None:
    data = tmp_path / "data"
    data.mkdir()
    with pytest.raises(UnsafePathError, match="inside data root"):
        Paths(source_root=data / "raw", data_root=data).validate()


def test_disjoint_roots_validate(tmp_path: Path) -> None:
    source = tmp_path / "raw"
    data = tmp_path / "data"
    source.mkdir()
    data.mkdir()
    paths = Paths(source_root=source, data_root=data)
    assert paths.validate() is paths
