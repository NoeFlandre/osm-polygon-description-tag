"""The CI acceptance-skip policy is explicit and locally permissive."""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from tests.acceptance.conftest import reject_ci_acceptance_skip


def _item(*, marked: bool) -> SimpleNamespace:
    return SimpleNamespace(
        nodeid="tests/acceptance/test_example.py::test_example",
        get_closest_marker=lambda name: object() if marked and name == "acceptance" else None,
    )


def _report() -> SimpleNamespace:
    return SimpleNamespace(skipped=True, outcome="skipped", longrepr=None)


def test_ci_acceptance_skip_is_reported_as_failure(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("CI", "true")
    report = _report()

    rejected = reject_ci_acceptance_skip(_item(marked=True), report)

    assert rejected is True
    assert report.outcome == "failed"
    assert "acceptance tests must not skip in CI" in str(report.longrepr)


def test_local_acceptance_skip_is_left_unchanged(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("CI", raising=False)
    report = _report()

    rejected = reject_ci_acceptance_skip(_item(marked=True), report)

    assert rejected is False
    assert report.outcome == "skipped"


def test_unmarked_ci_skip_is_left_unchanged(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("CI", "true")
    report = _report()

    rejected = reject_ci_acceptance_skip(_item(marked=False), report)

    assert rejected is False
    assert report.outcome == "skipped"
