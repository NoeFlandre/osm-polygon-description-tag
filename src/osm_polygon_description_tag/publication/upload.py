from __future__ import annotations

import subprocess
import time
from collections.abc import Callable
from pathlib import Path
from typing import Protocol

from osm_polygon_description_tag.dataset.manifest import file_sha256
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


def _verify_identity(plan: UploadPlan) -> None:
    for item in plan.files:
        path = Path(plan.data_root) / item.relative_path
        if path.is_symlink() or not path.is_file():
            raise PublicationError(f"artifact missing for upload: {path}")
        stat = path.stat()
        if stat.st_size != item.size_bytes:
            raise PublicationError(f"size drift for {path}")
        if file_sha256(path) != item.sha256:
            raise PublicationError(f"checksum drift for {path}")


def _build_command(plan: UploadPlan) -> list[str]:
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
    completed = getattr(error, "completed", None)
    if completed is None and isinstance(error, subprocess.CalledProcessError):
        completed = error
    if completed is None:
        return False, None, "exception"
    returncode = getattr(completed, "returncode", None)
    if isinstance(returncode, int) and returncode in RETRYABLE_EXIT_CODES:
        return True, returncode, "exit_code"
    output = (getattr(completed, "stderr", b"") or b"").decode("utf-8", errors="replace").lower()
    if "timeout" in output:
        return True, returncode, "timeout"
    return False, returncode, "exit_code"


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
    :func:`subprocess.run`. KeyboardInterrupt always escapes immediately
    without retry.
    """
    attempt = 0
    delay = backoff_seconds

    def _invoke() -> None:
        if _runner is None:
            subprocess.run(  # noqa: S603 - controlled argument array, no shell
                command,
                check=True,
                shell=False,
                timeout=timeout,
            )
            return
        _runner(command, timeout)

    while True:
        try:
            _invoke()
            return
        except KeyboardInterrupt:
            raise
        except subprocess.CalledProcessError as error:
            retryable, exit_code, kind = _classify_failure(error)
            if not retryable or attempt >= max_retries:
                raise
            attempt += 1
            bounded_delay = min(delay, backoff_cap_seconds)
            if retry_observer is not None:
                retry_observer(
                    attempt=attempt,
                    kind=kind,
                    exit_code=exit_code,
                    delay_seconds=bounded_delay,
                )
            time.sleep(bounded_delay)
            delay *= backoff_factor
        except subprocess.TimeoutExpired:
            if attempt >= max_retries:
                raise
            attempt += 1
            bounded_delay = min(delay, backoff_cap_seconds)
            if retry_observer is not None:
                retry_observer(
                    attempt=attempt,
                    kind="timeout",
                    exit_code=None,
                    delay_seconds=bounded_delay,
                )
            time.sleep(bounded_delay)
            delay *= backoff_factor


_default_runner_with_retry: _RetryRunner = _run_with_retry


def execute_upload(
    plan: UploadPlan,
    *,
    confirmation: str | None = None,
    runner: Runner | None = None,
    timeout: float | None = None,
    retry_observer: Callable[..., None] | None = None,
) -> None:
    """Execute the upload only after the exact plan identity is confirmed.

    ``confirmation`` is compared to the freshly computed plan identity from
    the same plan instance. A wrong or missing confirmation is refused
    before any command is executed. ``timeout`` is forwarded to the default
    runner; callers that inject ``runner`` are responsible for honoring it.

    The ``--include`` list is derived strictly from the plan's items (no
    wildcards), so previously uploaded artifacts are not re-sent.
    """
    if confirmation is None:
        raise PublicationError("confirmation required (must match freshly computed plan identity)")
    if confirmation != plan.identity_sha256:
        raise PublicationError("confirmation does not match plan identity (refusing to upload)")
    _verify_identity(plan)
    command = _build_command(plan)
    if runner is None:
        try:
            _default_runner_with_retry(
                command,
                timeout=timeout,
                retry_observer=retry_observer,
            )
        except TypeError as error:
            # Compatibility for injected legacy runners used by embedders.
            if "unexpected keyword argument 'retry_observer'" not in str(error):
                raise
            _default_runner_with_retry(command, timeout=timeout)
    else:
        runner(command)
