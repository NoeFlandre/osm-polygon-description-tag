"""Contract tests for the canonical UTC timestamp helper."""

from datetime import UTC, datetime

from osm_polygon_description_tag import runtime
from osm_polygon_description_tag.dataset import stats
from osm_polygon_description_tag.workflow import build


def test_all_timestamp_callers_use_the_canonical_runtime_helper() -> None:
    """Legacy module paths expose one implementation, preventing clock drift."""
    assert runtime.utc_now_iso is stats.utc_now_iso
    assert runtime.utc_now_iso is build.utc_now_iso


def test_utc_now_iso_is_timezone_aware_utc() -> None:
    parsed = datetime.fromisoformat(runtime.utc_now_iso())

    assert parsed.tzinfo is UTC
    assert parsed.utcoffset().total_seconds() == 0
