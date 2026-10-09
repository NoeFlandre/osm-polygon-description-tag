"""Shared conversions for persisted dataset measurements."""

from __future__ import annotations

from collections.abc import Sequence
from typing import Any, cast


def coerce_float_values(values: Sequence[object]) -> tuple[float, ...] | None:
    """Convert persisted numeric values, returning ``None`` on bad input."""
    try:
        return tuple(float(cast(Any, value)) for value in values)
    except (TypeError, ValueError):
        return None
