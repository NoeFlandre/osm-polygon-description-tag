"""Small, dependency-free time helpers used across the pipeline."""

from datetime import UTC, datetime


def utc_now() -> datetime:
    """Return the current instant as a timezone-aware UTC datetime.

    This is the only place the pipeline reads the wall clock. Callers import
    it rather than calling ``datetime.now`` themselves, so the clock has one
    definition to audit.
    """
    return datetime.now(UTC)


def utc_now_iso() -> str:
    """Return the current UTC time as an ISO-8601 string."""
    return utc_now().isoformat()


__all__ = ["utc_now", "utc_now_iso"]
