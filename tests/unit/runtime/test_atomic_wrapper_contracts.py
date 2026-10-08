"""Characterization tests for the atomic writers behind generated artifacts.

These tests pin the current bytes, no-op, failure and permission behaviour of
the writers that publication state, dataset docs and the dataset card use.
They describe what the code does today, so they must pass without changes to
production code.
"""

from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

import osm_polygon_description_tag.dataset.docs as docs_module
import osm_polygon_description_tag.dataset.geography.card as card_module
import osm_polygon_description_tag.publication.state as state_module

OLD_MTIME_NS = 1_700_000_000_000_000_000


@pytest.fixture
def work_dir(tmp_path: Path) -> Path:
    """Return an empty directory for the writer under test.

    The autouse guard in ``tests/conftest.py`` drops a fake ``hf`` file into
    ``tmp_path``, so listings are taken from this subdirectory instead.
    """
    directory = tmp_path / "work"
    directory.mkdir()
    return directory


def _names(directory: Path) -> list[str]:
    return sorted(entry.name for entry in directory.iterdir())


def _pin_mtime(path: Path) -> None:
    """Move mtime far into the past so any rewrite is visible as a change."""
    os.utime(path, ns=(OLD_MTIME_NS, OLD_MTIME_NS))


def test_publication_state_writer_emits_sorted_pretty_utf8_json(work_dir: Path) -> None:
    payload = {"zeta": 1, "name": "café", "nested": {"b": [2, 1], "a": {"y": "é", "x": None}}}
    target = work_dir / "missing" / "deeper" / "publication-state.json"

    state_module._atomic_write_json(target, payload)

    expected = (json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n").encode(
        "utf-8"
    )
    actual = target.read_bytes()
    assert actual == expected
    assert b"\r" not in actual
    assert _names(target.parent) == ["publication-state.json"]


def test_atomic_write_if_changed_first_write_creates_parents(work_dir: Path) -> None:
    target = work_dir / "a" / "b" / "out.bin"

    assert docs_module._atomic_write_if_changed(target, b"first") is True

    assert target.read_bytes() == b"first"
    assert _names(target.parent) == ["out.bin"]


def test_atomic_write_if_changed_identical_bytes_keep_inode_and_mtime(work_dir: Path) -> None:
    target = work_dir / "out.bin"
    assert docs_module._atomic_write_if_changed(target, b"same") is True
    _pin_mtime(target)
    before = target.stat()

    assert docs_module._atomic_write_if_changed(target, b"same") is False

    after = target.stat()
    assert after.st_ino == before.st_ino
    assert after.st_mtime_ns == before.st_mtime_ns
    assert _names(work_dir) == ["out.bin"]


def test_atomic_write_if_changed_different_bytes_replace_content(work_dir: Path) -> None:
    target = work_dir / "out.bin"
    target.write_bytes(b"old")

    assert docs_module._atomic_write_if_changed(target, b"new bytes") is True

    assert target.read_bytes() == b"new bytes"
    assert _names(work_dir) == ["out.bin"]


def test_write_if_changed_text_wrapper_writes_utf8_and_keeps_unchanged_file(
    work_dir: Path,
) -> None:
    target = work_dir / "README.md"

    assert docs_module._write_if_changed(target, "héllo") is True

    assert target.read_bytes() == b"h\xc3\xa9llo"
    _pin_mtime(target)
    before = target.stat()
    assert docs_module._write_if_changed(target, "héllo") is False
    after = target.stat()
    assert after.st_ino == before.st_ino
    assert after.st_mtime_ns == before.st_mtime_ns


def test_atomic_write_if_changed_failed_rename_keeps_old_bytes_and_no_temp_file(
    work_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    target = work_dir / "out.txt"
    target.write_bytes(b"old bytes")

    def failing_replace(self: Path, destination: object) -> Path:
        raise OSError("simulated rename failure")

    monkeypatch.setattr(Path, "replace", failing_replace)

    with pytest.raises(OSError, match="simulated rename failure"):
        docs_module._atomic_write_if_changed(target, b"new bytes")

    monkeypatch.undo()
    assert target.read_bytes() == b"old bytes"
    assert _names(work_dir) == ["out.txt"]


_CARD_LF = (
    "---\ntitle: café\n---\n\nIntro text.\n\n"
    "<!-- GENERATED:STATS:START -->\nstats\n<!-- GENERATED:STATS:END -->\n"
)
_CARD_WITH_MAP_LF = (
    "---\ntitle: café\n---\n\nIntro text.\n\n"
    "<!-- GENERATED:H3_MAP:START -->\n"
    "![H3 density of canonical globally unique `(osm_type, osm_id)` polygons "
    "with successfully extracted trimmed non-empty description text]"
    "(assets/description_polygon_density.png)\n"
    "<!-- GENERATED:H3_MAP:END -->\n"
    "<!-- GENERATED:STATS:START -->\nstats\n<!-- GENERATED:STATS:END -->\n"
)


def test_write_map_block_marker_to_template_inserts_block_before_stats(work_dir: Path) -> None:
    template = work_dir / "card.md"
    template.write_bytes(_CARD_LF.encode("utf-8"))

    card_module.write_map_block_marker_to_template(template)

    assert template.read_bytes() == _CARD_WITH_MAP_LF.encode("utf-8")
    assert _names(work_dir) == ["card.md"]


def test_write_map_block_marker_to_template_writes_crlf_input_back_as_lf(work_dir: Path) -> None:
    # Current behaviour: the template is read with universal newlines, so a CRLF
    # template is written back with LF endings. This pins that behaviour.
    template = work_dir / "card.md"
    template.write_bytes(_CARD_LF.replace("\n", "\r\n").encode("utf-8"))

    card_module.write_map_block_marker_to_template(template)

    actual = template.read_bytes()
    assert actual == _CARD_WITH_MAP_LF.encode("utf-8")
    assert b"\r" not in actual
    assert _names(work_dir) == ["card.md"]


def test_atomic_write_if_changed_new_file_mode_matches_plain_write(work_dir: Path) -> None:
    control = work_dir / "control.txt"
    control.write_bytes(b"control")
    target = work_dir / "out.txt"

    docs_module._atomic_write_if_changed(target, b"payload")

    assert target.stat().st_mode & 0o777 == control.stat().st_mode & 0o777
