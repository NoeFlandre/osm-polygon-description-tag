"""Shared compact labels for polygon counts in geography plots."""

from __future__ import annotations


def format_count_tick(value: float, _position: int | None = None) -> str:
    """Format a polygon count as a compact integer, thousands, or millions label."""
    count = round(value)
    if count < 1_000:
        return str(count)
    if count < 1_000_000:
        thousands = count / 1_000.0
        return f"{thousands:.0f}k" if thousands.is_integer() else f"{thousands:.1f}k"
    millions = count / 1_000_000.0
    return f"{millions:.1f}M"
