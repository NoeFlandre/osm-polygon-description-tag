"""Small, dependency-free time helpers used across the pipeline."""

from datetime import UTC, datetime


def utc_now_iso() -> str:
    """Return the current UTC time as an ISO-8601 string."""
    return datetime.now(UTC).isoformat()


__all__ = ["utc_now_iso"]
