"""Plan, submit, and reconcile one scheduler submission behind its durable intent."""

from collections.abc import Iterator
from dataclasses import dataclass, replace
from datetime import UTC, datetime
from pathlib import Path

from osm_polygon_description_tag.dataset.languages.checkpoint import (
    shard_paths,
)
from osm_polygon_description_tag.runtime.atomic import (
    atomic_write_json,
)
from osm_polygon_description_tag.workflow.grid_models import (
    BUNDLE_FILENAME,
    INTENT_FILENAME,
    GridOperatorError,
    JobBundle,
    JobNameResolver,
    JobPaths,
    SubmissionIntent,
    read_bundle,
    read_intent,
)
from osm_polygon_description_tag.workflow.grid_policy import (
    MAX_WALLTIME_SECONDS,
    REQUIRED_CORES,
    PolicyVerdict,
    policy_evidence_is_fresh,
)
from osm_polygon_description_tag.workflow.grid_prepare import (
    _existing_bundle,
    _read_job_config,
)
from osm_polygon_description_tag.workflow.grid_scheduler import (
    DEFAULT_COMMAND_TIMEOUT,
    CommandRunner,
    JobState,
    SchedulerError,
    SubmissionOutcome,
    SubmissionRequest,
    SubmissionResult,
    build_oarsub_argv,
    job_state,
    resolve_account_job_name,
    run_command,
    submit,
)
from osm_polygon_description_tag.workflow.grid_state import (
    _require_bound_checkpoint,
    _shard_checkpoint,
    submission_lock,
)


@dataclass(frozen=True, slots=True)
class SubmissionPlan:
    """Exactly what a submission would do, before any gate is opened."""

    bundle_id: str
    shard: str
    job_name: str
    walltime_seconds: int
    cores: int
    argv: tuple[str, ...]
    policy: PolicyVerdict
    attempt: int
    blocked_reason: str | None

    @property
    def may_apply(self) -> bool:
        """Return whether an apply gate would be honoured."""
        return self.blocked_reason is None and self.policy.may_submit

    def to_payload(self) -> dict[str, object]:
        return {
            "bundle_id": self.bundle_id,
            "shard": self.shard,
            "job_name": self.job_name,
            "walltime_seconds": self.walltime_seconds,
            "cores": self.cores,
            "argv": list(self.argv),
            "policy": self.policy.to_payload(),
            "attempt": self.attempt,
            "may_apply": self.may_apply,
            "blocked_reason": self.blocked_reason,
        }


def _existing_intent_block(
    paths: JobPaths, *, max_attempts: int | None = None
) -> tuple[str | None, int]:
    _validate_max_attempts(max_attempts)
    if not paths.intent.is_file():
        return None, 1
    intent = read_intent(paths.intent)
    blocked_reason = _intent_retry_block(intent, max_attempts)
    if blocked_reason is not None:
        return blocked_reason, intent.attempt
    return None, intent.attempt + 1


def _validate_max_attempts(value: int | None) -> None:
    if value is not None and (type(value) is not int or value < 1):
        raise GridOperatorError("maximum submission attempts must be a positive integer or null")


def _intent_retry_block(intent: SubmissionIntent, max_attempts: int | None) -> str | None:
    if intent.outcome == str(SubmissionOutcome.REJECTED):
        return _attempt_limit_reason(intent, max_attempts)
    if intent.is_active_or_ambiguous:
        return _active_intent_reason(intent)
    if intent.result_complete:
        return "complete results have already been acknowledged for this shard"
    if not intent.result_acknowledged:
        return "terminal job results must be collected and acknowledged before retrying"
    return _attempt_limit_reason(intent, max_attempts)


def _attempt_limit_reason(intent: SubmissionIntent, max_attempts: int | None) -> str | None:
    if max_attempts is not None and intent.attempt >= max_attempts:
        return f"maximum of {max_attempts} attempts has been reached for this shard"
    return None


def _active_intent_reason(intent: SubmissionIntent) -> str:
    if intent.job_id is not None:
        return f"job {intent.job_id} was already submitted for this bundle; reconcile it first"
    return (
        "a previous submission left an unresolved intent; "
        "reconcile whether a job exists before submitting again"
    )


def _run_wide_intent_block(paths: JobPaths, bundle: JobBundle) -> str | None:
    for _candidate, intent in _iter_intents(paths.run_dir):
        # ``_iter_intents`` refuses an intent that does not bind to its own directory's
        # bundle, and a job directory is named after its bundle, so this also skips
        # this job's own intent file.
        if intent.bundle_id == bundle.bundle_id:
            continue
        if intent.is_active_or_ambiguous:
            return (
                f"another shard has an active or ambiguous submission "
                f"({intent.shard}, bundle {intent.bundle_id[:16]}); reconcile it first"
            )
    return None


def _iter_intents(run_dir: Path) -> Iterator[tuple[Path, SubmissionIntent]]:
    jobs_root = run_dir / "jobs"
    if not jobs_root.exists():
        return
    _require_jobs_root(jobs_root)
    for candidate in sorted(jobs_root.iterdir()):
        _require_job_directory(candidate)
        found = _read_intent_candidate(candidate)
        if found is not None:
            yield found


def _require_jobs_root(jobs_root: Path) -> None:
    if jobs_root.is_symlink() or not jobs_root.is_dir():
        raise GridOperatorError(f"jobs directory is not a regular directory: {jobs_root}")


def _require_job_directory(candidate: Path) -> None:
    if candidate.is_symlink() or not candidate.is_dir():
        raise GridOperatorError(f"job directory must be a regular directory: {candidate}")


def _read_intent_candidate(candidate: Path) -> tuple[Path, SubmissionIntent] | None:
    intent_path = _intent_file_if_present(candidate)
    if intent_path is None:
        return None
    intent = read_intent(intent_path)
    candidate_bundle = _candidate_bundle(candidate, intent_path)
    _verify_intent_binding(candidate_bundle, intent, intent_path)
    return intent_path, intent


def _intent_file_if_present(candidate: Path) -> Path | None:
    intent_path = candidate / INTENT_FILENAME
    if intent_path.is_symlink():
        raise GridOperatorError(f"submission intent must be a regular file: {intent_path}")
    if not intent_path.exists():
        return None
    if not intent_path.is_file():
        raise GridOperatorError(f"submission intent must be a regular file: {intent_path}")
    return intent_path


def _verify_intent_binding(
    candidate_bundle: JobBundle, intent: SubmissionIntent, intent_path: Path
) -> None:
    if candidate_bundle.bundle_id != intent.bundle_id:
        raise GridOperatorError(f"submission intent is not bound to its bundle: {intent_path}")
    if candidate_bundle.shard != intent.shard:
        raise GridOperatorError(f"submission intent is not bound to its bundle: {intent_path}")


def _candidate_bundle(candidate: Path, intent_path: Path) -> JobBundle:
    bundle_path = candidate / BUNDLE_FILENAME
    if bundle_path.is_symlink() or not bundle_path.is_file():
        raise GridOperatorError(f"submission intent has no bound bundle: {intent_path}")
    return read_bundle(bundle_path)


def _policy_freshness_block(policy: PolicyVerdict, moment: datetime) -> str | None:
    captured_at = policy.evidence.captured_at
    if captured_at is None:
        return "policy evidence freshness is unknown"
    try:
        fresh = policy_evidence_is_fresh(moment, captured_at)
    except ValueError:
        return "policy evidence freshness is unknown"
    return None if fresh else "policy evidence is stale or from the future"


def plan_submission(
    paths: JobPaths,
    bundle: JobBundle,
    *,
    policy: PolicyVerdict,
    allowed_root: Path,
    walltime_seconds: int = MAX_WALLTIME_SECONDS,
    night_noretry: bool = True,
    max_attempts: int | None = None,
    require_fresh_policy: bool = False,
    now: datetime | None = None,
    queue: str | None = None,
) -> SubmissionPlan:
    """Describe the submission without contacting the scheduler."""
    _validate_prepared_submission(paths, bundle, walltime_seconds)
    request = _submission_request(bundle, walltime_seconds, night_noretry, paths, queue)
    blocked_reason, attempt = _submission_block(
        paths,
        bundle,
        max_attempts,
        require_fresh_policy,
        policy,
        now,
    )
    return SubmissionPlan(
        bundle_id=bundle.bundle_id,
        shard=bundle.shard,
        job_name=request.name,
        walltime_seconds=request.walltime_seconds,
        cores=request.cores,
        argv=build_oarsub_argv(request, allowed_root=allowed_root),
        policy=policy,
        attempt=attempt,
        blocked_reason=blocked_reason,
    )


def _validate_prepared_submission(
    paths: JobPaths, bundle: JobBundle, walltime_seconds: int
) -> None:
    _validate_submission_walltime(walltime_seconds)
    stored = _existing_bundle(paths)
    if stored is None or stored != bundle:
        raise GridOperatorError("prepared bundle does not match the requested submission")
    config = _read_job_config(paths.config, bundle)
    if config["walltime_seconds"] != walltime_seconds:
        raise GridOperatorError("submission walltime differs from prepared job config")


def _validate_submission_walltime(value: object) -> None:
    if type(value) is not int or value <= 0:
        raise GridOperatorError("walltime must be a positive number of seconds for this workload")
    if value > MAX_WALLTIME_SECONDS:
        raise GridOperatorError(
            f"walltime must not exceed {MAX_WALLTIME_SECONDS} seconds for this workload"
        )


def _submission_block(
    paths: JobPaths,
    bundle: JobBundle,
    max_attempts: int | None,
    require_fresh_policy: bool,
    policy: PolicyVerdict,
    now: datetime | None,
) -> tuple[str | None, int]:
    blocked_reason, attempt = _existing_intent_block(paths, max_attempts=max_attempts)
    if blocked_reason is None:
        blocked_reason = _run_wide_intent_block(paths, bundle)
    if blocked_reason is None:
        blocked_reason = _shard_state_block(paths, bundle, require_fresh_policy, policy, now)
    return blocked_reason, attempt


def _shard_state_block(
    paths: JobPaths,
    bundle: JobBundle,
    require_fresh_policy: bool,
    policy: PolicyVerdict,
    now: datetime | None,
) -> str | None:
    """Block on the shard's own state, then on policy freshness if required."""
    blocked_reason = _initial_checkpoint_block(paths, bundle)
    if blocked_reason is None and require_fresh_policy:
        blocked_reason = _policy_freshness_block(policy, now or datetime.now(UTC))
    return blocked_reason


def _initial_checkpoint_block(paths: JobPaths, bundle: JobBundle) -> str | None:
    try:
        checkpoint = _shard_checkpoint(shard_paths(paths.run_dir, bundle.shard))
    except GridOperatorError as error:
        return str(error)
    if checkpoint is None:
        return "no initial shard checkpoint exists; prepare this job again before submitting"
    try:
        _require_bound_checkpoint(checkpoint, bundle)
    except GridOperatorError as error:
        return str(error)
    return None


def _submission_request(
    bundle: JobBundle,
    walltime_seconds: int,
    night_noretry: bool,
    paths: JobPaths,
    queue: str | None = None,
) -> SubmissionRequest:
    """Build the request; the walltime is validated before this is reached.

    ``queue`` is a per-site input because Grid'5000 sites disagree about what a
    bare submission means: several auto-select a queue that does not exist and
    reject the job outright, while others refuse an explicit one.
    """
    return SubmissionRequest(
        script=paths.script,
        walltime_seconds=walltime_seconds,
        cores=REQUIRED_CORES,
        name=f"lang-{bundle.bundle_id[:16]}",
        night_noretry=night_noretry,
        queue=queue,
    )


def submit_job(
    paths: JobPaths,
    bundle: JobBundle,
    *,
    policy: PolicyVerdict,
    allowed_root: Path,
    apply: bool = False,
    walltime_seconds: int = MAX_WALLTIME_SECONDS,
    night_noretry: bool = True,
    max_attempts: int | None = None,
    require_fresh_policy: bool = False,
    runner: CommandRunner = run_command,
    now: datetime | None = None,
    queue: str | None = None,
) -> tuple[SubmissionPlan, SubmissionResult | None]:
    """Submit one job only behind an explicit gate and a passing policy verdict.

    The intent is written before ``oarsub`` runs, so an ambiguous scheduler
    response still leaves durable evidence that a job may exist.
    """
    if not apply:
        return (
            plan_submission(
                paths,
                bundle,
                policy=policy,
                allowed_root=allowed_root,
                walltime_seconds=walltime_seconds,
                night_noretry=night_noretry,
                max_attempts=max_attempts,
                require_fresh_policy=require_fresh_policy,
                now=now,
                queue=queue,
            ),
            None,
        )
    with submission_lock(paths.run_dir):
        plan = plan_submission(
            paths,
            bundle,
            policy=policy,
            allowed_root=allowed_root,
            walltime_seconds=walltime_seconds,
            night_noretry=night_noretry,
            max_attempts=max_attempts,
            require_fresh_policy=True,
            now=now,
            queue=queue,
        )
        if not plan.may_apply:
            return plan, None
        intent = SubmissionIntent(
            bundle_id=bundle.bundle_id,
            shard=bundle.shard,
            job_name=plan.job_name,
            walltime_seconds=plan.walltime_seconds,
            cores=plan.cores,
            recorded_at=(now or datetime.now(UTC)).isoformat(),
            attempt=plan.attempt,
        )
        atomic_write_json(paths.intent, intent.to_payload())
        request = _submission_request(bundle, walltime_seconds, night_noretry, paths, queue)
        result = submit(request, allowed_root=allowed_root, runner=runner)
        _record_outcome(paths, intent, result)
        return plan, result


def _record_outcome(paths: JobPaths, intent: SubmissionIntent, result: SubmissionResult) -> None:
    atomic_write_json(
        paths.intent,
        replace(
            intent,
            job_id=result.job_id,
            outcome=str(result.outcome),
            detail=result.detail,
        ).to_payload(),
    )


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
