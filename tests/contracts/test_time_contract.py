"""Contract tests for the canonical UTC timestamp helpers."""

from datetime import UTC, datetime
from pathlib import Path

from osm_polygon_description_tag import runtime
from osm_polygon_description_tag.runtime import time as runtime_time
from osm_polygon_description_tag.workflow import build

SOURCE_ROOT = Path(__file__).parents[2] / "src" / "osm_polygon_description_tag"
RUNTIME_TIME = SOURCE_ROOT / "runtime" / "time.py"


def test_all_timestamp_callers_use_the_canonical_runtime_helper() -> None:
    """Legacy module paths expose one implementation, preventing clock drift."""
    assert runtime.utc_now_iso is build.utc_now_iso


def test_utc_now_iso_is_timezone_aware_utc() -> None:
    parsed = datetime.fromisoformat(runtime.utc_now_iso())

    assert parsed.tzinfo is UTC
    assert parsed.utcoffset().total_seconds() == 0


def test_utc_now_is_timezone_aware_utc_and_current() -> None:
    before = datetime.now(UTC)
    moment = runtime_time.utc_now()
    after = datetime.now(UTC)

    assert moment.tzinfo is UTC
    assert before <= moment <= after


def test_only_runtime_time_reads_the_wall_clock() -> None:
    offenders = [
        path.relative_to(SOURCE_ROOT.parent.parent)
        for path in SOURCE_ROOT.rglob("*.py")
        if path != RUNTIME_TIME and "datetime.now" in path.read_text(encoding="utf-8")
    ]

    assert offenders == []
