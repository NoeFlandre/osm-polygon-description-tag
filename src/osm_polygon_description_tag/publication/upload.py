from __future__ import annotations

import os
import subprocess
import sys
import threading
import time
from collections import deque
from collections.abc import Callable
from pathlib import Path
from typing import BinaryIO, Protocol, cast

from osm_polygon_description_tag.dataset.manifest import file_sha256
from osm_polygon_description_tag.publication.hub_client import (
    commit_operation_add,
    create_dataset_commit,
    new_hf_api,
)
from osm_polygon_description_tag.publication.models import (
    DEFAULT_BACKOFF_CAP_SECONDS,
    DEFAULT_BACKOFF_FACTOR,
    DEFAULT_BACKOFF_SECONDS,
    DEFAULT_MAX_RETRIES,
    RETRYABLE_EXIT_CODES,
    PublicationError,
    Runner,
    UploadPlan,
)

_STDERR_CHUNK_BYTES = 4096
_MAX_STDERR_BYTES = 64 * 1024
_MAX_STDERR_CHUNKS = _MAX_STDERR_BYTES // _STDERR_CHUNK_BYTES
_STDERR_POLL_SECONDS = 0.01
_STDERR_JOIN_TIMEOUT_SECONDS = 0.25


def _verify_identity(plan: UploadPlan) -> None:
    for item in plan.files:
        _verify_item_identity(plan, item.relative_path, item.size_bytes, item.sha256)


def _verify_item_identity(plan: UploadPlan, relative_path: str, size: int, checksum: str) -> None:
    path = Path(plan.data_root) / relative_path
    if path.is_symlink() or not path.is_file():
        raise PublicationError(f"artifact missing for upload: {path}")
    stat = path.stat()
    if stat.st_size != size:
        raise PublicationError(f"size drift for {path}")
    if file_sha256(path) != checksum:
        raise PublicationError(f"checksum drift for {path}")


def build_command(plan: UploadPlan) -> list[str]:
    """Build an ``hf upload-large-folder`` command from the plan's exact items.

    ``--include`` flags are derived strictly from ``plan.files`` (in
    deterministic order). No wildcards are used; previously uploaded
    artifacts are not re-sent.
    """
    command = [
        "hf",
        "upload-large-folder",
        plan.repo_id,
        plan.data_root,
        "--repo-type",
        "dataset",
    ]
    for item in plan.files:
        command.extend(["--include", item.relative_path])
    return command


def _classify_failure(
    error: object,
) -> tuple[bool, int | None, str]:
    """Return (retryable, exit_code, kind) for a subprocess error."""
    completed = _completed_process(error)
    if completed is None:
        return False, None, "exception"
    returncode = getattr(completed, "returncode", None)
    if isinstance(returncode, int) and returncode in RETRYABLE_EXIT_CODES:
        return True, returncode, "exit_code"
    if _contains_timeout(completed):
        return True, returncode, "timeout"
    return False, returncode, "exit_code"


def _completed_process(error: object) -> object | None:
    completed = getattr(error, "completed", None)
    if completed is None and isinstance(error, subprocess.CalledProcessError):
        return error
    return completed


class _CompletedProcessWithStderr(Protocol):
    stderr: bytes | None


def _contains_timeout(completed: object) -> bool:
    try:
        stderr = cast(
            _CompletedProcessWithStderr, completed
        ).stderr  # pragma: no mutate - static narrowing only
    except AttributeError:
        return False
    if not stderr:
        return False
    error_handler = "replace"  # pragma: no mutate - canonical handler name
    output = stderr.decode(errors=error_handler).lower()
    return "timeout" in output


class _RetryRunner(Protocol):
    def __call__(
        self,
        command: list[str],
        *,
        max_retries: int = DEFAULT_MAX_RETRIES,
        backoff_seconds: float = DEFAULT_BACKOFF_SECONDS,
        backoff_factor: float = DEFAULT_BACKOFF_FACTOR,
        backoff_cap_seconds: float = DEFAULT_BACKOFF_CAP_SECONDS,
        timeout: float | None = None,
        _runner: Callable[[list[str], float | None], None] | None = None,
        retry_observer: Callable[..., None] | None = None,
    ) -> None: ...


def _run_with_retry(
    command: list[str],
    *,
    max_retries: int = DEFAULT_MAX_RETRIES,
    backoff_seconds: float = DEFAULT_BACKOFF_SECONDS,
    backoff_factor: float = DEFAULT_BACKOFF_FACTOR,
    backoff_cap_seconds: float = DEFAULT_BACKOFF_CAP_SECONDS,
    timeout: float | None = None,
    _runner: Callable[[list[str], float | None], None] | None = None,
    retry_observer: Callable[..., None] | None = None,
) -> None:
    """Default ``hf`` runner with bounded exponential backoff on retryable errors.

    ``timeout`` defaults to ``None`` (no overall timeout) so healthy resumable
    uploads are not killed at five minutes. Callers may pass a positive value
    for explicit termination.

    ``_runner`` is a private hook for tests; production code uses
    :func:`subprocess.Popen`. KeyboardInterrupt always escapes immediately
    without retry.
    """
    attempt = 0
    delay = backoff_seconds

    while True:
        try:
            _invoke_runner(command, timeout, _runner)
            return
        except (subprocess.CalledProcessError, subprocess.TimeoutExpired) as error:
            retryable, exit_code, kind = _failure_details(error)
            decision = _called_error_retry(
                retryable,
                attempt,
                max_retries,
                delay,
                backoff_cap_seconds,
            )
            if decision is None:
                raise
            attempt, delay = _sleep_before_retry(
                attempt,
                backoff_factor,
                decision,
                kind,
                exit_code,
                retry_observer,
            )


def _failure_details(
    error: subprocess.CalledProcessError | subprocess.TimeoutExpired,
) -> tuple[bool, int | None, str]:
    if isinstance(error, subprocess.TimeoutExpired):
        return True, None, "timeout"
    return _classify_failure(error)


def _invoke_runner(
    command: list[str],
    timeout: float | None,
    runner: Callable[[list[str], float | None], None] | None,
) -> None:
    if runner is None:
        _run_subprocess(command, timeout)
        return
    runner(command, timeout)


def _run_subprocess(command: list[str], timeout: float | None) -> None:
    retained_stderr = _StderrTail()
    stop_reader = threading.Event()
    with subprocess.Popen(  # noqa: S603 - controlled argument array, no shell
        command,
        shell=False,
        stderr=subprocess.PIPE,
    ) as process:
        stderr_pipe = cast(BinaryIO, process.stderr)  # pragma: no mutate - static narrowing only
        reader = threading.Thread(
            target=_drain_stderr,
            args=(stderr_pipe, retained_stderr, stop_reader),
            daemon=True,
            name="upload-stderr-reader",
        )
        reader.start()
        timeout_error: subprocess.TimeoutExpired | None = None
        try:
            return_code = process.wait(timeout=timeout)
        except subprocess.TimeoutExpired as error:
            process.kill()
            process.wait()
            timeout_error = error
        except KeyboardInterrupt:
            process.kill()
            process.wait()
            raise
        finally:
            stop_reader.set()
            reader.join(timeout=_STDERR_JOIN_TIMEOUT_SECONDS)

    stderr = retained_stderr.snapshot()
    if timeout_error is not None:
        timeout_error.stderr = stderr
        raise timeout_error
    if return_code:
        raise subprocess.CalledProcessError(return_code, command, stderr=stderr)


class _StderrTail:
    def __init__(self) -> None:
        self._chunks: deque[bytes] = deque(maxlen=_MAX_STDERR_CHUNKS)
        self._lock = threading.Lock()

    def append(self, chunk: bytes) -> None:
        with self._lock:
            self._chunks.append(chunk)

    def snapshot(self) -> bytes:
        with self._lock:
            return b"".join(self._chunks)


def _drain_stderr(pipe: BinaryIO, retained: _StderrTail, stop_reader: threading.Event) -> None:
    """Forward child stderr while retaining only a bounded tail for errors."""
    try:
        descriptor = pipe.fileno()
        os.set_blocking(
            descriptor, False
        )  # pragma: no mutate - None also means nonblocking to os.set_blocking
    except (OSError, ValueError):
        return
    with pipe:
        forwarding = True
        while chunk := _read_stderr_chunk(descriptor, stop_reader):
            retained.append(chunk)
            if forwarding:
                try:
                    _write_stderr_chunk(chunk)
                except (OSError, UnicodeError, ValueError):
                    forwarding = False  # pragma: no mutate - any false sentinel has same effect


def _read_stderr_chunk(descriptor: int, stop_reader: threading.Event) -> bytes | None:
    while True:
        chunk = _try_read_stderr_chunk(descriptor)
        if chunk is not None:
            return chunk
        if stop_reader.wait(_STDERR_POLL_SECONDS):
            return _try_read_stderr_chunk(descriptor)


def _try_read_stderr_chunk(descriptor: int) -> bytes | None:
    try:
        return os.read(descriptor, _STDERR_CHUNK_BYTES)
    except BlockingIOError:
        return None
    except OSError:
        return b""


def _write_stderr_chunk(chunk: bytes) -> None:
    stream = sys.stderr
    if stream is None:
        return
    binary_stream = getattr(stream, "buffer", None)
    if binary_stream is None:
        stream.write(chunk.decode(errors="replace"))
    else:
        binary_stream.write(chunk)
    stream.flush()


def _called_error_retry(
    retryable: bool,
    attempt: int,
    max_retries: int,
    delay: float,
    cap: float,
) -> tuple[float, float] | None:
    if not retryable or attempt >= max_retries:
        return None
    return (min(delay, cap), delay)


def _sleep_before_retry(
    attempt: int,
    factor: float,
    decision: tuple[float, float],
    kind: str,
    exit_code: int | None,
    retry_observer: Callable[..., None] | None,
) -> tuple[int, float]:
    attempt += 1  # pragma: no mutate - retry attempts are one-based state
    bounded_delay, current_delay = decision
    if retry_observer is not None:
        retry_observer(
            attempt=attempt,
            kind=kind,
            exit_code=exit_code,
            delay_seconds=bounded_delay,
        )
    time.sleep(bounded_delay)
    return attempt, current_delay * factor


def default_runner_with_retry(
    command: list[str],
    *,
    max_retries: int = DEFAULT_MAX_RETRIES,
    backoff_seconds: float = DEFAULT_BACKOFF_SECONDS,
    backoff_factor: float = DEFAULT_BACKOFF_FACTOR,
    backoff_cap_seconds: float = DEFAULT_BACKOFF_CAP_SECONDS,
    timeout: float | None = None,
    _runner: Callable[[list[str], float | None], None] | None = None,
    retry_observer: Callable[..., None] | None = None,
) -> None:
    """Run an HF upload command with bounded, retryable failure handling."""
    _run_with_retry(
        command,
        max_retries=max_retries,
        backoff_seconds=backoff_seconds,
        backoff_factor=backoff_factor,
        backoff_cap_seconds=backoff_cap_seconds,
        timeout=timeout,
        _runner=_runner,
        retry_observer=retry_observer,
    )


def _dispatch_upload(
    plan: UploadPlan,
    command: list[str],
    runner: Runner | None,
    timeout: float | None,
    retry_observer: Callable[..., None] | None,
    parent_revision: str | None,
) -> None:
    if runner is not None:
        runner(command)
        return
    if parent_revision is not None:
        _run_parented_metadata_commit(plan, parent_revision)
        return
    _run_default_upload(command, timeout, retry_observer)


def execute_upload(
    plan: UploadPlan,
    *,
    confirmation: str | None = None,
    runner: Runner | None = None,
    timeout: float | None = None,
    retry_observer: Callable[..., None] | None = None,
    parent_revision: str | None = None,
) -> None:
    """Execute the upload only after the exact plan identity is confirmed.

    ``confirmation`` is compared to the freshly computed plan identity from
    the same plan instance. A wrong or missing confirmation is refused
    before any command is executed. ``timeout`` is forwarded to the default
    runner; callers that inject ``runner`` are responsible for honoring it.

    The ``--include`` list is derived strictly from the plan's items (no
    wildcards), so previously uploaded artifacts are not re-sent.
    """
    _require_confirmation(plan, confirmation)
    _verify_identity(plan)
    command = build_command(plan)
    try:
        _dispatch_upload(plan, command, runner, timeout, retry_observer, parent_revision)
    except subprocess.CalledProcessError as error:
        raise PublicationError(f"upload failed with exit code {error.returncode}") from error
    except subprocess.TimeoutExpired as error:
        raise PublicationError(f"upload timed out after {error.timeout} seconds") from error


def _require_confirmation(plan: UploadPlan, confirmation: str | None) -> None:
    if confirmation is None:
        raise PublicationError("confirmation required (must match freshly computed plan identity)")
    if confirmation != plan.identity_sha256:
        raise PublicationError("confirmation does not match plan identity (refusing to upload)")


def _run_parented_metadata_commit(plan: UploadPlan, parent_revision: str) -> None:
    """Commit metadata with an optimistic parent revision.

    The large-folder CLI can target a revision but does not expose the
    ``parent_commit`` guard needed to prevent a concurrent language/card
    publication from being overwritten. Metadata plans are small, so the Hub
    commit API is the safe production path when an anchor is available.
    """
    try:
        api = new_hf_api()
        operations = [
            commit_operation_add(item.relative_path, Path(plan.data_root) / item.relative_path)
            for item in plan.files
        ]
        create_dataset_commit(
            api,
            repo_id=plan.repo_id,
            operations=operations,
            commit_message="Update deterministic dataset statistics",
            parent_commit=parent_revision,
        )
    except Exception as error:
        raise PublicationError(
            f"metadata commit failed or remote revision changed: {error}"
        ) from error


def _run_default_upload(
    command: list[str], timeout: float | None, retry_observer: Callable[..., None] | None
) -> None:
    try:
        default_runner_with_retry(command, timeout=timeout, retry_observer=retry_observer)
    except TypeError as error:
        # Compatibility for injected legacy runners used by embedders.
        if "unexpected keyword argument 'retry_observer'" not in str(error):
            raise
        default_runner_with_retry(command, timeout=timeout)
