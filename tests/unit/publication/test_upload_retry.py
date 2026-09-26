"""Retry and timeout behaviour of ``default_runner_with_retry``.

One test per behaviour: a retryable failure is retried, a non-retryable one
is attempted once, a timeout is retried and then propagated, Ctrl-C is never
retried, the retry observer is told about each retry, and no hard timeout
kills a slow upload. Failure classification is observed only through the
public runner.
"""

from __future__ import annotations

import subprocess
import time

import pytest

from osm_polygon_description_tag.publication import (
    PublicationError,
    default_runner_with_retry,
)


def _failing_once(error: BaseException) -> tuple[list[int], object]:
    calls: list[int] = []

    def runner(_command: list[str], _timeout: float | None) -> None:
        calls.append(len(calls))
        if len(calls) == 1:
            raise error

    return calls, runner


def _always_failing(error: BaseException) -> tuple[list[int], object]:
    calls: list[int] = []

    def runner(_command: list[str], _timeout: float | None) -> None:
        calls.append(len(calls))
        # A wrong stop predicate must not hang the process; fail loudly instead.
        if len(calls) > 10:
            raise AssertionError("runner retried without bound")
        raise error

    return calls, runner


@pytest.mark.parametrize(
    ("returncode", "stderr"),
    [
        (5, None),
        (429, None),
        (502, None),
        (503, None),
        (504, None),
        (1, b"connection timeout"),
        (1, b"Read TIMEOUT from hub"),
    ],
)
def test_a_retryable_failure_is_retried_until_it_succeeds(
    returncode: int, stderr: bytes | None
) -> None:
    calls, runner = _failing_once(subprocess.CalledProcessError(returncode, ["hf"], stderr=stderr))

    default_runner_with_retry(["hf"], max_retries=3, backoff_seconds=0.0, _runner=runner)  # type: ignore[arg-type]

    assert len(calls) == 2


@pytest.mark.parametrize(
    ("returncode", "stderr"),
    [
        (1, None),
        (2, None),
        (3, None),
        (4, None),
        (1, b""),
        (2, b"permission denied"),
    ],
)
def test_a_non_retryable_failure_is_attempted_once(returncode: int, stderr: bytes | None) -> None:
    calls, runner = _always_failing(
        subprocess.CalledProcessError(returncode, ["hf"], stderr=stderr)
    )

    with pytest.raises(subprocess.CalledProcessError) as caught:
        default_runner_with_retry(["hf"], max_retries=3, backoff_seconds=0.0, _runner=runner)  # type: ignore[arg-type]

    assert caught.value.returncode == returncode
    assert len(calls) == 1


def test_an_error_carrying_its_completed_process_is_classified_from_it() -> None:
    """``error.completed`` wins over the error's own exit code."""
    error = subprocess.CalledProcessError(1, ["hf"])
    error.completed = subprocess.CompletedProcess(["hf"], returncode=429)  # type: ignore[attr-defined]
    calls, runner = _failing_once(error)

    default_runner_with_retry(["hf"], max_retries=3, backoff_seconds=0.0, _runner=runner)  # type: ignore[arg-type]

    assert len(calls) == 2


def test_a_retryable_failure_gives_up_after_max_retries() -> None:
    calls, runner = _always_failing(subprocess.CalledProcessError(503, ["hf"]))

    with pytest.raises(subprocess.CalledProcessError):
        default_runner_with_retry(["hf"], max_retries=2, backoff_seconds=0.0, _runner=runner)  # type: ignore[arg-type]

    assert len(calls) == 3


def test_a_real_command_failure_is_classified_from_its_exit_code() -> None:
    """Without an injected runner, ``subprocess.run`` drives the same policy."""
    with pytest.raises(subprocess.CalledProcessError):
        default_runner_with_retry(["/bin/sh", "-c", "exit 1"], max_retries=2, backoff_seconds=0.0)
    default_runner_with_retry(["/bin/sh", "-c", "exit 0"], max_retries=1, backoff_seconds=0.0)


def test_a_timeout_is_retried_then_propagated_once_the_budget_is_spent() -> None:
    timeout = subprocess.TimeoutExpired(cmd=["hf"], timeout=0.1)
    calls, runner = _failing_once(timeout)
    default_runner_with_retry(["hf"], max_retries=3, backoff_seconds=0.0, _runner=runner)  # type: ignore[arg-type]
    assert len(calls) == 2

    calls, runner = _always_failing(timeout)
    with pytest.raises(subprocess.TimeoutExpired):
        default_runner_with_retry(["hf"], max_retries=2, backoff_seconds=0.0, _runner=runner)  # type: ignore[arg-type]
    assert len(calls) == 3


def test_keyboard_interrupt_is_never_retried() -> None:
    calls, runner = _always_failing(KeyboardInterrupt())

    with pytest.raises(KeyboardInterrupt):
        default_runner_with_retry(["hf"], max_retries=3, timeout=None, _runner=runner)  # type: ignore[arg-type]

    assert len(calls) == 1


def test_a_publication_error_from_the_runner_propagates_unretried() -> None:
    calls, runner = _always_failing(PublicationError("hub rejected"))

    with pytest.raises(PublicationError, match="hub rejected"):
        default_runner_with_retry(["hf"], max_retries=2, backoff_seconds=0.0, _runner=runner)  # type: ignore[arg-type]

    assert len(calls) == 1


def test_the_retry_observer_receives_attempt_classification_and_delay() -> None:
    events: list[dict[str, object]] = []
    _, runner = _failing_once(subprocess.CalledProcessError(503, ["hf"], stderr=b"temporary"))

    default_runner_with_retry(
        ["hf"],
        max_retries=1,
        backoff_seconds=0,
        _runner=runner,  # type: ignore[arg-type]
        retry_observer=lambda **fields: events.append(fields),
    )

    assert events == [{"attempt": 1, "kind": "exit_code", "exit_code": 503, "delay_seconds": 0}]


def test_the_retry_observer_reports_a_stderr_timeout_as_a_timeout() -> None:
    events: list[dict[str, object]] = []
    _, runner = _failing_once(subprocess.CalledProcessError(1, ["hf"], stderr=b"read timeout"))

    default_runner_with_retry(
        ["hf"],
        max_retries=1,
        backoff_seconds=0,
        _runner=runner,  # type: ignore[arg-type]
        retry_observer=lambda **fields: events.append(fields),
    )

    assert events == [{"attempt": 1, "kind": "timeout", "exit_code": 1, "delay_seconds": 0}]


def test_a_long_running_upload_is_not_killed_at_300_seconds() -> None:
    """No hard timeout is imposed: the runner receives ``timeout=None``."""
    seen: list[float | None] = []

    def runner(_command: list[str], timeout: float | None) -> None:
        seen.append(timeout)
        time.sleep(0.05)

    default_runner_with_retry(["hf"], max_retries=1, _runner=runner)

    assert seen == [None]
