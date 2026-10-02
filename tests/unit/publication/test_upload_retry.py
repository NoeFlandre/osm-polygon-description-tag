"""Retry and timeout behaviour of ``default_runner_with_retry``.

One test per behaviour: a retryable failure is retried, a non-retryable one
is attempted once, a timeout is retried and then propagated, Ctrl-C is never
retried, the retry observer is told about each retry, and no hard timeout
kills a slow upload. Failure classification is observed only through the
public runner.
"""

from __future__ import annotations

import subprocess
import sys
import threading
import time
from pathlib import Path
from typing import Any

import pytest

from osm_polygon_description_tag.publication import (
    PublicationError,
    default_runner_with_retry,
    upload,
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


def _capture_stderr_reader_join_timeouts(
    monkeypatch: pytest.MonkeyPatch,
) -> list[float | None]:
    join_timeouts: list[float | None] = []
    real_thread = threading.Thread

    def capture_thread(*args: Any, **kwargs: Any) -> threading.Thread:
        reader = real_thread(*args, **kwargs)
        if kwargs.get("name") == "upload-stderr-reader":
            real_join = reader.join

            def record_join(*join_args: Any, **join_kwargs: Any) -> None:
                timeout_arg = join_kwargs.get("timeout", join_args[0] if join_args else None)
                join_timeouts.append(timeout_arg)
                real_join(*join_args, **join_kwargs)

            reader.join = record_join  # type: ignore[method-assign]
        return reader

    monkeypatch.setattr(threading, "Thread", capture_thread)
    return join_timeouts


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


def test_a_real_non_retryable_command_failure_is_attempted_once(tmp_path: Path) -> None:
    """A real subprocess with an ordinary failure is not retried."""
    attempts_file = tmp_path / "attempts"
    script = """
from pathlib import Path
import sys

attempts_file = Path(sys.argv[1])
attempt = int(attempts_file.read_text()) + 1 if attempts_file.exists() else 1
attempts_file.write_text(str(attempt))
raise SystemExit(1)
"""
    events: list[dict[str, object]] = []

    with pytest.raises(subprocess.CalledProcessError):
        default_runner_with_retry(
            [sys.executable, "-c", script, str(attempts_file)],
            max_retries=2,
            backoff_seconds=0.0,
            retry_observer=lambda **fields: events.append(fields),
        )
    assert attempts_file.read_text() == "1"
    assert events == []

    default_runner_with_retry([sys.executable, "-c", "pass"], max_retries=1, backoff_seconds=0.0)


def test_a_real_child_timeout_on_stderr_is_retried_until_it_succeeds(
    tmp_path: Path, capfd: pytest.CaptureFixture[str]
) -> None:
    attempts_file = tmp_path / "attempts"
    script = """
from pathlib import Path
import sys

attempts_file = Path(sys.argv[1])
attempt = int(attempts_file.read_text()) + 1 if attempts_file.exists() else 1
attempts_file.write_text(str(attempt))
if attempt < 3:
    print("connection timeout", file=sys.stderr, flush=True)
    raise SystemExit(1)
print("upload succeeded", file=sys.stderr, flush=True)
"""
    events: list[dict[str, object]] = []

    default_runner_with_retry(
        [sys.executable, "-c", script, str(attempts_file)],
        max_retries=2,
        backoff_seconds=0.0,
        retry_observer=lambda **fields: events.append(fields),
    )

    assert attempts_file.read_text() == "3"
    assert events == [
        {"attempt": 1, "kind": "timeout", "exit_code": 1, "delay_seconds": 0.0},
        {"attempt": 2, "kind": "timeout", "exit_code": 1, "delay_seconds": 0.0},
    ]
    live_stderr = capfd.readouterr().err
    assert live_stderr.count("connection timeout") == 2
    assert "upload succeeded" in live_stderr


def test_a_real_flushed_stderr_message_is_forwarded_before_child_exit(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    release_file = tmp_path / "release"
    finished_file = tmp_path / "finished"
    script = """
from pathlib import Path
import sys
import time

release_file = Path(sys.argv[1])
finished_file = Path(sys.argv[2])
print("child ready", file=sys.stderr, flush=True)
while not release_file.exists():
    time.sleep(0.01)
finished_file.write_text("done")
"""

    class SignalingSink:
        def __init__(self) -> None:
            self.ready = threading.Event()
            self.text = ""

        def write(self, text: str) -> int:
            self.text += text
            self.ready.set()
            return len(text)

        def flush(self) -> None:
            return None

    sink = SignalingSink()
    monkeypatch.setattr(sys, "stderr", sink)
    completed = threading.Event()
    errors: list[Exception] = []

    def run_child() -> None:
        try:
            default_runner_with_retry(
                [sys.executable, "-c", script, str(release_file), str(finished_file)],
                max_retries=0,
            )
        except BaseException as error:  # noqa: BLE001 - transfer worker failures to the test
            errors.append(error)
        finally:
            completed.set()

    runner_thread = threading.Thread(target=run_child, daemon=True)
    runner_thread.start()
    try:
        forwarded_before_exit = sink.ready.wait(timeout=1.0)
        child_still_waiting = not finished_file.exists()
    finally:
        release_file.touch()
        assert completed.wait(timeout=3.0)
        runner_thread.join(timeout=0.25)

    assert forwarded_before_exit
    assert child_still_waiting
    assert sink.text == "child ready\n"
    assert errors == []
    assert not runner_thread.is_alive()


def test_timeout_returns_while_a_descendant_keeps_stderr_open(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    release_file = tmp_path / "release-descendant"
    ready_file = tmp_path / "descendant-ready"
    heartbeat_file = tmp_path / "descendant-heartbeat"
    finished_file = tmp_path / "descendant-finished"
    descendant_script = """
from pathlib import Path
import sys
import time

release_file = Path(sys.argv[1])
ready_file = Path(sys.argv[2])
heartbeat_file = Path(sys.argv[3])
finished_file = Path(sys.argv[4])
ready_file.write_text("ready")
while not release_file.exists():
    with heartbeat_file.open("a") as heartbeat:
        heartbeat.write("x")
    time.sleep(0.01)
finished_file.write_text("done")
"""
    script = """
from pathlib import Path
import subprocess
import sys
import time

descendant_script = sys.argv[1]
ready_file = Path(sys.argv[3])
subprocess.Popen(
    [
        sys.executable,
        "-c",
        descendant_script,
        sys.argv[2],
        sys.argv[3],
        sys.argv[4],
        sys.argv[5],
    ]
)
while not ready_file.exists():
    time.sleep(0.01)
print("outer started", file=sys.stderr, flush=True)
time.sleep(30)
"""
    command = [
        sys.executable,
        "-c",
        script,
        descendant_script,
        str(release_file),
        str(ready_file),
        str(heartbeat_file),
        str(finished_file),
    ]
    completed = threading.Event()
    errors: list[Exception] = []
    join_timeouts = _capture_stderr_reader_join_timeouts(monkeypatch)

    def run_child() -> None:
        try:
            default_runner_with_retry(command, max_retries=0, timeout=0.75)
        except BaseException as error:  # noqa: BLE001 - transfer worker failures to the test
            errors.append(error)
        finally:
            completed.set()

    runner_thread = threading.Thread(target=run_child, daemon=True)
    started = time.monotonic()
    runner_thread.start()
    try:
        returned_before_release = completed.wait(timeout=2.0)
        elapsed = time.monotonic() - started
        descendant_started = ready_file.exists()
        heartbeat_before = len(heartbeat_file.read_text()) if heartbeat_file.exists() else 0
        time.sleep(0.05)
        descendant_holds_pipe = (
            heartbeat_file.exists()
            and len(heartbeat_file.read_text()) > heartbeat_before
            and not finished_file.exists()
        )
        reader_stopped = not any(
            thread.name == "upload-stderr-reader" and thread.is_alive()
            for thread in threading.enumerate()
        )
    finally:
        release_file.touch()
        assert completed.wait(timeout=3.0)
        runner_thread.join(timeout=0.25)

    assert returned_before_release
    assert elapsed < 2.0
    assert descendant_started
    assert descendant_holds_pipe
    assert reader_stopped
    assert join_timeouts == [upload._STDERR_JOIN_TIMEOUT_SECONDS]
    assert len(errors) == 1
    assert isinstance(errors[0], subprocess.TimeoutExpired)
    assert errors[0].stderr == b"outer started\n"
    deadline = time.monotonic() + 2.0
    while not finished_file.exists() and time.monotonic() < deadline:
        time.sleep(0.01)
    assert finished_file.exists()
    assert finished_file.read_text() == "done"
    assert not runner_thread.is_alive()


def test_a_real_command_error_keeps_only_a_bounded_stderr_tail(
    capfd: pytest.CaptureFixture[str],
) -> None:
    script = (
        "import sys; "
        "sys.stderr.write('x' * 131072); "
        "sys.stderr.write('\\nconnection timeout tail\\n'); "
        "sys.exit(1)"
    )
    command = [sys.executable, "-c", script]

    with pytest.raises(subprocess.CalledProcessError) as caught:
        default_runner_with_retry(command, max_retries=0, backoff_seconds=0.0, timeout=1.0)

    assert isinstance(caught.value.stderr, bytes)
    assert len(caught.value.stderr) <= 64 * 1024
    assert len(caught.value.stderr) == 15 * 4096 + len(b"\nconnection timeout tail\n")
    assert caught.value.cmd == command
    assert caught.value.stderr.endswith(b"connection timeout tail\n")
    live_stderr = capfd.readouterr().err
    assert live_stderr.endswith("connection timeout tail\n")
    assert len(live_stderr) > len(caught.value.stderr)


def test_a_real_process_timeout_is_retried(
    tmp_path: Path, capfd: pytest.CaptureFixture[str]
) -> None:
    attempts_file = tmp_path / "attempts"
    script = """
from pathlib import Path
import sys
import time

attempts_file = Path(sys.argv[1])
attempt = int(attempts_file.read_text()) + 1 if attempts_file.exists() else 1
attempts_file.write_text(str(attempt))
print(f"attempt {attempt}", file=sys.stderr, flush=True)
if attempt == 1:
    time.sleep(5)
"""
    events: list[dict[str, object]] = []

    default_runner_with_retry(
        [sys.executable, "-c", script, str(attempts_file)],
        max_retries=1,
        backoff_seconds=0.0,
        timeout=0.25,
        retry_observer=lambda **fields: events.append(fields),
    )

    assert attempts_file.read_text() == "2"
    assert events == [{"attempt": 1, "kind": "timeout", "exit_code": None, "delay_seconds": 0.0}]
    assert capfd.readouterr().err == "attempt 1\nattempt 2\n"


def test_a_real_process_timeout_retains_stderr_when_retries_are_exhausted(
    capfd: pytest.CaptureFixture[str],
) -> None:
    command = [
        sys.executable,
        "-c",
        "import sys, time; print('upload started', file=sys.stderr, flush=True); time.sleep(5)",
    ]

    with pytest.raises(subprocess.TimeoutExpired) as caught:
        default_runner_with_retry(command, max_retries=0, timeout=0.25)

    assert caught.value.stderr == b"upload started\n"
    assert capfd.readouterr().err == "upload started\n"


def test_a_keyboard_interrupt_from_a_real_process_is_not_retried(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    guarded_popen = subprocess.Popen
    wait_calls = 0
    processes: list[object] = []

    class InterruptingProcess:
        def __init__(self, command: list[str], **kwargs: object) -> None:
            self._process = guarded_popen(command, **kwargs)
            self.stderr = self._process.stderr
            processes.append(self)

        def __enter__(self) -> InterruptingProcess:
            return self

        def __exit__(self, *_args: object) -> bool:
            return self._process.__exit__(*_args)

        def wait(self, timeout: float | None = None) -> int:
            nonlocal wait_calls
            wait_calls += 1
            if wait_calls == 1:
                raise KeyboardInterrupt
            return self._process.wait(timeout=timeout)

        def kill(self) -> None:
            self._process.kill()

    monkeypatch.setattr(subprocess, "Popen", InterruptingProcess)
    events: list[dict[str, object]] = []

    with pytest.raises(KeyboardInterrupt):
        default_runner_with_retry(
            [sys.executable, "-c", "import time; time.sleep(30)"],
            max_retries=2,
            backoff_seconds=0.0,
            retry_observer=lambda **fields: events.append(fields),
        )

    assert len(processes) == 1
    assert wait_calls == 2
    assert events == []


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
