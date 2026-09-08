"""Portable JSON identities and crash-safe replacement contracts."""

import os
import stat
from pathlib import Path

import pytest

from osm_polygon_description_tag.dataset.languages.atomic import (
    atomic_write_bytes,
    atomic_write_via,
    canonical_json_bytes,
)


def test_canonical_json_is_identical_for_reordered_unicode_payloads() -> None:
    expected = '{"a":{"é":true},"z":1}\n'.encode()

    assert canonical_json_bytes({"z": 1, "a": {"é": True}}) == expected
    assert canonical_json_bytes({"a": {"é": True}, "z": 1}) == expected


def test_replacement_syncs_content_before_rename_and_directory_afterward(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    destination = tmp_path / "nested" / "state.json"
    events = []
    replace = os.replace

    def sync(fd: int) -> None:
        events.append("directory" if stat.S_ISDIR(os.fstat(fd).st_mode) else "file")

    def rename(source: Path, target: Path) -> None:
        assert source.parent == destination.parent
        assert source.read_bytes() == b"complete"
        events.append("rename")
        replace(source, target)

    monkeypatch.setattr(os, "fsync", sync)
    monkeypatch.setattr(os, "replace", rename)

    atomic_write_bytes(destination, b"complete")

    assert events == ["file", "rename", "directory"]
    assert destination.read_bytes() == b"complete"
    assert list(destination.parent.iterdir()) == [destination]


def test_failed_producer_preserves_previous_state_and_removes_partial_file(tmp_path: Path) -> None:
    destination = tmp_path / "owned" / "state.json"
    destination.parent.mkdir()
    destination.write_bytes(b"previous")

    def produce(temp: Path) -> None:
        temp.write_bytes(b"partial")
        raise OSError("interrupted producer")

    with pytest.raises(OSError, match="interrupted producer"):
        atomic_write_via(destination, produce)

    assert destination.read_bytes() == b"previous"
    assert list(destination.parent.iterdir()) == [destination]
