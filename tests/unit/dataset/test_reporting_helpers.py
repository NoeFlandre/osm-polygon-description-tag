"""Unchanged-file behavior of the dataset-card byte writer."""

from __future__ import annotations

import os
from pathlib import Path

from osm_polygon_description_tag.dataset.docs import _atomic_write_if_changed

OLD_MTIME_NS = 1_700_000_000_000_000_000


def test_atomic_write_if_changed_preserves_file_when_bytes_match(tmp_path: Path) -> None:
    target = tmp_path / "out.txt"
    target.write_text("hello", encoding="utf-8")
    os.utime(target, ns=(OLD_MTIME_NS, OLD_MTIME_NS))
    assert _atomic_write_if_changed(target, b"hello") is False
    assert target.stat().st_mtime_ns == OLD_MTIME_NS
    assert target.read_text(encoding="utf-8") == "hello"
