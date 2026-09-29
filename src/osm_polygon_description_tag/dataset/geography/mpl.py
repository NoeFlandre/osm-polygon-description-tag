"""Lazy matplotlib access for the dataset-card renderers.

Importing pyplot costs ~200 ms, so the CLI must not pay for it unless a
command actually renders. The Agg backend is selected before the first
pyplot import, exactly once.
"""

from __future__ import annotations

from functools import lru_cache
from typing import Any


@lru_cache(maxsize=1)
def pyplot() -> Any:
    """Return ``matplotlib.pyplot`` configured with the non-interactive backend."""
    import matplotlib

    matplotlib.use("Agg")  # non-interactive backend for CI/macOS terminal runs
    import matplotlib.pyplot as plt

    return plt


__all__ = ["pyplot"]
