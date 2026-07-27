from pathlib import Path

import pytest

from osm_polygon_description_tag.discovery import Source, _output_name_for, discover_sources


def test_discovery_is_direct_sorted_and_pbf_only(tmp_path: Path) -> None:
    (tmp_path / "z-latest.osm.pbf").touch()
    (tmp_path / "a-latest.osm.pbf").touch()
    (tmp_path / "notes.txt").touch()
    nested = tmp_path / "nested"
    nested.mkdir()
    (nested / "ignored.osm.pbf").touch()

    found = discover_sources(tmp_path)

    assert [item.name for item in found] == ["a-latest.osm.pbf", "z-latest.osm.pbf"]
    assert [item.output_name for item in found] == ["a-latest.parquet", "z-latest.parquet"]
    assert all(isinstance(item, Source) for item in found)


def test_discovery_captures_source_identity(tmp_path: Path) -> None:
    path = tmp_path / "region.osm.pbf"
    path.write_bytes(b"data")
    stat = path.stat()

    found = discover_sources(tmp_path)

    assert len(found) == 1
    assert found[0].path == path
    assert found[0].name == "region.osm.pbf"
    assert found[0].size_bytes == stat.st_size
    assert found[0].mtime_ns == stat.st_mtime_ns


def test_discovery_rejects_missing_directory(tmp_path: Path) -> None:
    with pytest.raises(NotADirectoryError):
        discover_sources(tmp_path / "missing")


def test_discovery_excludes_symlinks(tmp_path: Path) -> None:
    real = tmp_path / "real.osm.pbf"
    real.write_bytes(b"data")
    (tmp_path / "link.osm.pbf").symlink_to(real)

    found = discover_sources(tmp_path)

    assert [item.name for item in found] == ["real.osm.pbf"]


def test_discovery_empty_directory_returns_empty_tuple(tmp_path: Path) -> None:
    assert discover_sources(tmp_path) == ()


def test_output_name_helper_strips_exact_osm_pbf_suffix() -> None:
    assert _output_name_for("afghanistan-latest.osm.pbf") == "afghanistan-latest.parquet"
    assert _output_name_for("a.osm.pbf") == "a.parquet"
    # The exact suffix removal is injective: distinct inputs never share an output.
    assert _output_name_for("x.osm.pbf") != _output_name_for("x.osm.osm.pbf")


def test_discovery_does_not_falsely_flag_distinct_files(tmp_path: Path) -> None:
    # Distinct direct children map to distinct outputs; the collision guard must not
    # spuriously fire for legitimately distinct names that share a substring.
    (tmp_path / "x.osm.pbf").touch()
    (tmp_path / "x.osm.osm.pbf").touch()

    found = discover_sources(tmp_path)

    assert sorted(item.output_name for item in found) == ["x.osm.parquet", "x.parquet"]
