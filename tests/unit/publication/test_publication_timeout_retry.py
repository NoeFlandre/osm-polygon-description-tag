"""RED tests for the TimeoutExpired retry path inside the publication runner.

The amendment exercises the same retry semantics on ``TimeoutExpired``
as on ``CalledProcessError``: classify the failure and retry with
backoff until ``max_retries`` is exhausted.
"""

from __future__ import annotations

import subprocess

import pytest

from osm_polygon_description_tag.publication import (
    PublicationError,
    _default_runner_with_retry,
)


def test_retry_observer_receives_attempt_classification_and_delay() -> None:
    events: list[dict[str, object]] = []
    attempts = 0

    def runner(_command: list[str], _timeout: float | None) -> None:
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            raise subprocess.CalledProcessError(503, ["hf"], stderr=b"temporary")

    _default_runner_with_retry(
        ["hf"],
        max_retries=1,
        backoff_seconds=0,
        _runner=runner,
        retry_observer=lambda **fields: events.append(fields),
    )
    assert events == [
        {
            "attempt": 1,
            "kind": "exit_code",
            "exit_code": 503,
            "delay_seconds": 0,
        }
    ]


def test_default_runner_retries_timeout_and_eventually_succeeds() -> None:
    """A runner that raises ``TimeoutExpired`` is retried until it succeeds."""

    calls: list[int] = []
    state = {"raised": False}

    def runner(command: list[str], timeout: float | None = None) -> None:
        calls.append(len(calls))
        if not state["raised"]:
            state["raised"] = True
            raise subprocess.TimeoutExpired(cmd=command, timeout=timeout or 0.1)
        return None

    _default_runner_with_retry(
        ["hf", "upload-large-folder"],
        _runner=runner,
        max_retries=3,
        backoff_seconds=0.0,
    )
    assert len(calls) == 2


def test_default_runner_propagates_timeout_after_max_retries() -> None:
    """Persistent ``TimeoutExpired`` escapes after the retry budget is exhausted."""

    calls = 0

    def runner(command: list[str], timeout: float | None = None) -> None:
        nonlocal calls
        calls += 1
        if calls > 3:
            # Keep an incorrect retry-stop predicate from hanging the mutation
            # process indefinitely after the expected timeout budget.
            raise subprocess.CalledProcessError(1, command)
        raise subprocess.TimeoutExpired(cmd=command, timeout=timeout or 0.1)

    with pytest.raises(subprocess.TimeoutExpired):
        _default_runner_with_retry(
            ["hf", "upload-large-folder"],
            _runner=runner,
            max_retries=2,
            backoff_seconds=0.0,
        )


def test_publication_error_is_wrapped_when_runner_raises_publication_error() -> None:
    """A direct ``PublicationError`` from the runner propagates unwrapped."""

    def runner(command: list[str], timeout: float | None = None) -> None:
        raise PublicationError("hub rejected")

    with pytest.raises(PublicationError, match="hub rejected"):
        _default_runner_with_retry(
            ["hf", "upload-large-folder"],
            _runner=runner,
            max_retries=2,
            backoff_seconds=0.0,
        )
