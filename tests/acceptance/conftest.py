"""Mark every test under ``tests/acceptance`` as an acceptance scenario."""

from __future__ import annotations

import pytest


def pytest_collection_modifyitems(items: list[pytest.Item]) -> None:
    for item in items:
        if "/tests/acceptance/" in str(item.path).replace("\\", "/"):
            item.add_marker(pytest.mark.acceptance)
