"""Unit tests for the reporting helpers exercised outside the full stats pipeline.

The amendment adds ``_safe_map`` and ``_write_if_changed`` which are
shared between ``stats.json`` generation and the dataset card. These
tests cover the helper branches without invoking the DuckDB-backed
collector.
"""

from __future__ import annotations

from pathlib import Path

from osm_polygon_description_tag.dataset.docs import _write_if_changed
from osm_polygon_description_tag.dataset.stats import _safe_map


def test_safe_map_returns_empty_for_none() -> None:
    assert _safe_map(None) == {}


def test_safe_map_handles_dict_input() -> None:
    assert _safe_map({"pt-BR": 1, "en": 2}) == {"pt-BR": 1, "en": 2}


def test_safe_map_returns_empty_for_unknown_input() -> None:
    assert _safe_map(["pt-BR"]) == {}


def test_safe_map_skips_none_keys_and_coerces_ints() -> None:
    assert _safe_map({None: 7, "en": "3"}) == {"en": 3}


def test_write_if_changed_writes_when_bytes_differ(tmp_path: Path) -> None:
    target = tmp_path / "out.txt"
    assert _write_if_changed(target, "hello") is True
    assert target.read_text(encoding="utf-8") == "hello"


def test_write_if_changed_preserves_file_when_bytes_match(tmp_path: Path) -> None:
    target = tmp_path / "out.txt"
    target.write_text("hello", encoding="utf-8")
    original_mtime = target.stat().st_mtime_ns
    assert _write_if_changed(target, "hello") is False
    assert target.stat().st_mtime_ns == original_mtime
    assert target.read_text(encoding="utf-8") == "hello"
