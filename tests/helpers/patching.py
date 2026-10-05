"""Patch a name in every module that really reads it."""

from __future__ import annotations

from types import ModuleType

import pytest


def patch_modules(
    monkeypatch: pytest.MonkeyPatch,
    targets: tuple[tuple[ModuleType, str], ...],
    value: object,
    *,
    raising: bool = True,
) -> None:
    """Set ``value`` on each ``(module, attribute)`` pair, failing if one is missing."""
    for module, name in targets:
        monkeypatch.setattr(module, name, value, raising=raising)
