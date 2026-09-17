"""Shared text-value predicates for extracted description artifacts."""

from __future__ import annotations

from typing import TypeGuard


def is_nonempty_text(value: object) -> TypeGuard[str]:
    """Return whether a value is a string with non-whitespace content."""
    return isinstance(value, str) and bool(value.strip())


__all__ = ["is_nonempty_text"]
