"""Grid operator reconciliation responsibilities."""

from __future__ import annotations

from dataclasses import dataclass, replace
from datetime import UTC, datetime
from pathlib import Path

from osm_polygon_description_tag.dataset.languages.atomic import atomic_write_json
from osm_polygon_description_tag.dataset.languages.validation import RunReport, validate_run
from osm_polygon_description_tag.workflow.grid_scheduler import (
    DEFAULT_COMMAND_TIMEOUT,
    CommandRunner,
    JobState,
    SchedulerError,
    job_state,
    resolve_account_job_name,
    run_command,
)

from .bundle import read_intent
from .models import GridOperatorError, JobNameResolver, JobPaths, SubmissionIntent
from .state import submission_lock


@dataclass(frozen=True, slots=True)
class Reconciliation:
    """What is known about a previously recorded submission."""

    intent: SubmissionIntent | None
    state: JobState
    detail: str

    @property
    def needs_operator_attention(self) -> bool:
        """Return whether a human must resolve this before any resubmission."""
        if self.intent is None:
            return False
        return self.intent.is_unresolved or self.state is JobState.UNKNOWN

    def to_payload(self) -> dict[str, object]:
        return {
            "intent": None if self.intent is None else self.intent.to_payload(),
            "state": str(self.state),
            "detail": self.detail,
            "needs_operator_attention": self.needs_operator_attention,
        }


def reconcile_job(
    paths: JobPaths,
    *,
    apply: bool = False,
    runner: CommandRunner = run_command,
    resolve_job_name: JobNameResolver | None = None,
    now: datetime | None = None,
) -> Reconciliation:
    """Identify an existing job before any further submission is considered."""
    if resolve_job_name is not None and not callable(resolve_job_name):
        raise GridOperatorError("job-name resolver must be callable")
    if not apply:
        return _dry_reconciliation(paths)
    with submission_lock(paths.run_dir):
        return _live_reconciliation(paths, runner, resolve_job_name, now)


def _dry_reconciliation(paths: JobPaths) -> Reconciliation:
    if not paths.intent.is_file():
        return Reconciliation(None, JobState.UNKNOWN, "no submission intent has been recorded")
    intent = read_intent(paths.intent)
    if intent.terminal_state == str(JobState.TERMINATED):
        return Reconciliation(intent, JobState.TERMINATED, "terminal state was already reconciled")
    if intent.job_id is None:
        return _missing_job_id_reconciliation(intent)
    return Reconciliation(intent, JobState.UNKNOWN, "pass the apply gate to query the scheduler")


def _missing_job_id_reconciliation(intent: SubmissionIntent) -> Reconciliation:
    return Reconciliation(
        intent,
        JobState.UNKNOWN,
        "intent has no job identifier; query the scheduler by job name before resubmitting",
    )


def resolve_job_name(
    job_name: str,
    *,
    runner: CommandRunner = run_command,
    timeout: float = DEFAULT_COMMAND_TIMEOUT,
) -> int | None:
    """Resolve one recorded account job by exact name using read-only OAR JSON.

    OAR's account listing is bounded by the jobs it still retains in its
    account view, including any recent terminal records it exposes. ``None``
    is an unresolved result, never permission to resubmit; site retention
    limits can therefore require manual operator investigation.
    """
    account_jobs = _account_lookup_output(runner, timeout)
    try:
        return resolve_account_job_name(account_jobs, job_name)
    except SchedulerError as error:
        raise GridOperatorError(f"cannot resolve account job by name: {error}") from error


def _account_lookup_output(runner: CommandRunner, timeout: float) -> str:
    argv = ("oarstat", "-u", "-J")
    try:
        result = runner(argv, timeout)
    except SchedulerError as error:
        raise GridOperatorError(f"cannot query account jobs for reconciliation: {error}") from error
    if result.timed_out:
        raise GridOperatorError("account job lookup timed out; do not resubmit")
    if result.returncode != 0:
        raise GridOperatorError(f"account job lookup exited {result.returncode}; do not resubmit")
    return "{}" if result.stdout == "" else result.stdout


def _live_reconciliation(
    paths: JobPaths,
    runner: CommandRunner,
    resolve_job_name: JobNameResolver | None,
    now: datetime | None,
) -> Reconciliation:
    if not paths.intent.is_file():
        return Reconciliation(None, JobState.UNKNOWN, "no submission intent has been recorded")
    intent = read_intent(paths.intent)
    resolved = _resolved_job(paths, intent, resolve_job_name)
    if isinstance(resolved, Reconciliation):
        return resolved
    bound, job_id = resolved
    state, detail = job_state(job_id, runner=runner)
    reconciled = _record_terminal_reconciliation(paths, bound, state, now)
    return Reconciliation(reconciled, state, detail)


def _resolved_job(
    paths: JobPaths,
    intent: SubmissionIntent,
    resolver: JobNameResolver | None,
) -> tuple[SubmissionIntent, int] | Reconciliation:
    """Return the intent bound to a known job id, or why it stays unresolved."""
    if intent.job_id is not None:
        return intent, intent.job_id
    if resolver is None:
        return _missing_job_id_reconciliation(intent)
    return _resolved_by_name(paths, intent, resolver)


def _resolved_by_name(
    paths: JobPaths, intent: SubmissionIntent, resolver: JobNameResolver
) -> tuple[SubmissionIntent, int] | Reconciliation:
    resolved_id = resolver(intent.job_name)
    if resolved_id is None:
        return Reconciliation(
            intent,
            JobState.UNKNOWN,
            "scheduler lookup by job name did not resolve a job; do not resubmit",
        )
    if type(resolved_id) is not int or resolved_id < 1:
        raise GridOperatorError("job-name resolver must return a positive integer or null")
    updated = replace(intent, job_id=resolved_id)
    atomic_write_json(paths.intent, updated.to_payload())
    return updated, resolved_id


def _record_terminal_reconciliation(
    paths: JobPaths,
    intent: SubmissionIntent,
    state: JobState,
    now: datetime | None,
) -> SubmissionIntent:
    if state is not JobState.TERMINATED:
        return intent
    updated = replace(
        intent,
        terminal_state=str(JobState.TERMINATED),
        reconciled_at=(now or datetime.now(UTC)).isoformat(),
    )
    atomic_write_json(paths.intent, updated.to_payload())
    return updated


def gather_policy_outputs(
    site: str,
    *,
    runner: CommandRunner = run_command,
    timeout: float = DEFAULT_COMMAND_TIMEOUT,
) -> tuple[str | None, str | None]:
    """Capture live usage-policy and home-quota output from a frontend.

    Each command that fails, times out, or is unavailable yields ``None`` so the
    policy evaluation treats its evidence as unknown rather than as approval.
    ``quota -l`` is deliberately not used: it omits NFS home storage.
    """
    if not isinstance(site, str) or not site or not site.replace("-", "").isalnum():
        raise GridOperatorError("site must be a simple alphanumeric name")
    usage = _captured(("usagepolicycheck", "-t", "--sites", site, "--json"), runner, timeout)
    quota = _captured(("quota", "-p", "-w"), runner, timeout)
    return usage, quota


def gather_policy_evidence(
    site: str,
    *,
    runner: CommandRunner = run_command,
    timeout: float = DEFAULT_COMMAND_TIMEOUT,
) -> tuple[str | None, str | None, str | None]:
    """Capture usage, home-quota, and account-wide job-state evidence.

    The third value is ``oarstat -u -J`` output, with successful zero-byte OAR2
    responses normalized to ``{}``. Callers must parse it with
    :func:`parse_account_job_states` and inject its active count into policy
    evaluation; the historical usage-policy ``total_jobs`` is never reused.
    """
    usage, quota = gather_policy_outputs(site, runner=runner, timeout=timeout)
    account_jobs = _captured(("oarstat", "-u", "-J"), runner, timeout)
    if account_jobs == "":
        account_jobs = "{}"
    return usage, quota, account_jobs


def _captured(argv: tuple[str, ...], runner: CommandRunner, timeout: float) -> str | None:
    try:
        result = runner(argv, timeout)
    except SchedulerError:
        return None
    if result.timed_out or result.returncode != 0:
        return None
    return result.stdout


def collect_results(run_dir: Path, shard: str) -> RunReport:
    """Validate what a completed or paused job actually produced."""
    return validate_run(run_dir, shards=(shard,))
