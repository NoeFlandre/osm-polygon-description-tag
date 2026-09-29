"""Acceptance-only pytest contracts."""

from __future__ import annotations

import os
from collections.abc import Generator

import pytest


def reject_ci_acceptance_skip(item: pytest.Item, report: pytest.TestReport) -> bool:
    """Turn a skipped acceptance item into a failure in CI, but allow local skips."""
    ci_enabled = os.environ.get("CI", "").strip().lower() in {"1", "true", "yes"}
    if not ci_enabled or item.get_closest_marker("acceptance") is None or not report.skipped:
        return False

    report.outcome = "failed"
    report.longrepr = f"{item.nodeid}: acceptance tests must not skip in CI"
    return True


@pytest.hookimpl(hookwrapper=True)
def pytest_runtest_makereport(
    item: pytest.Item, call: pytest.CallInfo[object]
) -> Generator[None, pytest.TestReport, None]:
    outcome = yield
    report = outcome.get_result()
    reject_ci_acceptance_skip(item, report)
