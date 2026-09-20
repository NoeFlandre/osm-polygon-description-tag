"""Transport, policy-evidence, and retrieval filesystem helpers."""

from __future__ import annotations

import shutil
from collections.abc import Sequence
from datetime import UTC, datetime
from pathlib import Path

from osm_polygon_description_tag.workflow.grid_operator import (
    GridOperatorError,
    gather_policy_evidence,
)
from osm_polygon_description_tag.workflow.grid_scheduler import (
    DEFAULT_COMMAND_TIMEOUT,
    CommandResult,
    CommandRunner,
    SchedulerError,
    account_active_job_count,
)
from osm_polygon_description_tag.workflow.grid_scheduler import (
    run_command as grid_command_runner,
)


def _execute_transport(argv: Sequence[str], runner: CommandRunner | None) -> CommandResult:
    """Run one explicit transport argv only after its caller opened ``--apply``."""
    command_runner = runner or grid_command_runner
    try:
        result = command_runner(argv, DEFAULT_COMMAND_TIMEOUT)
    except SchedulerError as error:
        raise GridOperatorError(f"transport command could not run: {error}") from error
    _raise_transport_failure(result)
    return result


def _raise_transport_failure(result: CommandResult) -> None:
    if result.timed_out:
        raise GridOperatorError("transport command timed out")
    if result.returncode != 0:
        detail = result.stderr.strip() or "no diagnostic"
        raise GridOperatorError(f"transport command exited {result.returncode}: {detail}")


def _transport_payload(
    argv: Sequence[str], result: CommandResult | None
) -> dict[str, object] | None:
    if result is None:
        return None
    return {
        "argv": list(argv),
        "returncode": result.returncode,
        "stderr": result.stderr,
        "stdout": result.stdout,
        "timed_out": result.timed_out,
    }


def _utc_now() -> datetime:
    """Return the current instant.

    This is the one clock the Grid handlers read, so a test can freeze it and
    evaluate the Europe/Paris day/night policy at a chosen moment.
    """
    return datetime.now(UTC)


def _capture_policy(
    site: str, *, runner: CommandRunner
) -> tuple[str | None, str | None, int | None, datetime]:
    """Capture all policy inputs and count only active account-wide jobs."""
    captured_at = _utc_now()
    usage_output, quota_output, account_job_output = gather_policy_evidence(site, runner=runner)
    if account_job_output is None:
        account_job_count = None
    else:
        try:
            account_job_count = account_active_job_count(account_job_output)
        except SchedulerError as error:
            raise GridOperatorError(
                f"account-wide scheduler evidence is invalid: {error}"
            ) from error
    return usage_output, quota_output, account_job_count, captured_at


def _portable_remote_paths(remote_bundle_dir: str) -> dict[str, str]:
    """Expose the exact remote paths required to reuse a staged job script."""
    base = remote_bundle_dir.rstrip("/") or "/"
    return {
        "remote_bundle_dir": base,
        "remote_project_dir": _remote_child(base, "project"),
        "remote_run_dir": _remote_child(base, "run"),
        "remote_source_dir": _remote_child(base, "source"),
    }


def _remote_child(base: str, name: str) -> str:
    return f"/{name}" if base == "/" else f"{base}/{name}"


def _seed_retrieval_snapshot(local_run_dir: Path, retrieved_run_dir: Path) -> None:
    """Give a shard-only retrieval the immutable snapshot needed for import."""
    source = _retrieval_snapshot_source(local_run_dir)
    _prepare_retrieval_directory(retrieved_run_dir)
    target = _retrieval_snapshot_target(retrieved_run_dir)
    if target.exists():
        return
    _copy_retrieval_snapshot(source, target)


def _retrieval_snapshot_source(local_run_dir: Path) -> Path:
    source = local_run_dir / "snapshot.json"
    if source.is_symlink() or not source.is_file():
        raise GridOperatorError(f"local run snapshot is missing: {source}")
    return source


def _prepare_retrieval_directory(retrieved_run_dir: Path) -> None:
    if retrieved_run_dir.is_symlink():
        raise GridOperatorError(
            f"retrieved run directory must not be a symlink: {retrieved_run_dir}"
        )
    retrieved_run_dir.mkdir(parents=True, exist_ok=True)


def _retrieval_snapshot_target(retrieved_run_dir: Path) -> Path:
    target = retrieved_run_dir / "snapshot.json"
    if target.is_symlink():
        raise GridOperatorError(f"retrieved snapshot must not be a symlink: {target}")
    return target


def _copy_retrieval_snapshot(source: Path, target: Path) -> None:
    try:
        shutil.copyfile(source, target)
    except OSError as error:
        raise GridOperatorError(f"cannot seed retrieved run snapshot: {error}") from error
