"""Unit tests for the reporting helpers exercised outside the full stats pipeline.

The amendment adds ``_safe_map`` and ``_write_if_changed`` which are
shared between ``stats.json`` generation and the dataset card. These
tests cover the helper branches without invoking the DuckDB-backed
collector.
"""

from __future__ import annotations

from pathlib import Path

import osm_polygon_description_tag.dataset.docs as docs
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


def test_text_and_binary_writers_share_atomic_byte_helper(tmp_path: Path, monkeypatch) -> None:
    calls: list[tuple[Path, bytes]] = []

    def fake_atomic_write(path: Path, data: bytes) -> bool:
        calls.append((path, data))
        return True

    monkeypatch.setattr(docs, "_atomic_write_if_changed", fake_atomic_write, raising=False)

    text_path = tmp_path / "out.txt"
    binary_path = tmp_path / "out.bin"
    assert docs._write_if_changed(text_path, "héllo") is True
    assert docs._write_bytes_if_changed(binary_path, b"\x00\x01") is True

    assert calls == [(text_path, "héllo".encode()), (binary_path, b"\x00\x01")]
