"""UTF-8 and unchanged-file behavior of the dataset-card text writer."""

from __future__ import annotations

from pathlib import Path

from osm_polygon_description_tag.dataset.docs import _write_if_changed


def test_write_if_changed_writes_when_bytes_differ(tmp_path: Path) -> None:
    target = tmp_path / "out.txt"
    assert _write_if_changed(target, "héllo") is True
    assert target.read_bytes() == b"h\xc3\xa9llo"


def test_write_if_changed_preserves_file_when_bytes_match(tmp_path: Path) -> None:
    target = tmp_path / "out.txt"
    target.write_text("hello", encoding="utf-8")
    original_mtime = target.stat().st_mtime_ns
    assert _write_if_changed(target, "hello") is False
    assert target.stat().st_mtime_ns == original_mtime
    assert target.read_text(encoding="utf-8") == "hello"
