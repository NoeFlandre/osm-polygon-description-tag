"""Temp-file construction for atomic writes lives only in ``runtime/atomic.py``."""

from pathlib import Path

import pytest

import osm_polygon_description_tag
from osm_polygon_description_tag.runtime.atomic import atomic_write_via

_SOURCE_ROOT = Path(osm_polygon_description_tag.__file__).parent
# Parquet writers stage a sibling file that a promotion step validates and
# fsyncs itself; they are not byte-payload writes and stay outside the helper.
_PARQUET_STAGING = (
    "dataset/storage.py",
    "dataset/migration.py",
    "dataset/text_migration.py",
)
_TEMP_MARKER = "uuid.uuid4().hex}.tmp"


def test_only_runtime_atomic_builds_atomic_temp_files() -> None:
    offenders = sorted(
        str(path.relative_to(_SOURCE_ROOT))
        for path in _SOURCE_ROOT.rglob("*.py")
        if _TEMP_MARKER in path.read_text(encoding="utf-8")
    )
    assert offenders == sorted(["runtime/atomic.py", *_PARQUET_STAGING])


def test_failed_atomic_write_removes_temp_and_keeps_destination(tmp_path: Path) -> None:
    destination = tmp_path / "state.json"
    destination.write_bytes(b"old")

    def produce(temp: Path) -> None:
        temp.write_bytes(b"partial")
        raise RuntimeError("boom")

    with pytest.raises(RuntimeError, match="boom"):
        atomic_write_via(destination, produce)

    assert destination.read_bytes() == b"old"
    assert sorted(p.name for p in tmp_path.glob(".*")) == []
