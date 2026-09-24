"""Grid operator submission responsibilities."""

from __future__ import annotations

from collections.abc import Iterator
from dataclasses import dataclass, replace
from datetime import UTC, datetime
from pathlib import Path

from osm_polygon_description_tag.dataset.languages.atomic import atomic_write_json
from osm_polygon_description_tag.dataset.languages.checkpoint import shard_paths
from osm_polygon_description_tag.workflow.grid_policy import (
    MAX_WALLTIME_SECONDS,
    REQUIRED_CORES,
    PolicyVerdict,
    policy_evidence_is_fresh,
)
from osm_polygon_description_tag.workflow.grid_scheduler import (
    CommandRunner,
    SubmissionOutcome,
    SubmissionRequest,
    SubmissionResult,
    build_oarsub_argv,
    run_command,
    submit,
)

from .bundle import (
    _existing_bundle,
    _read_job_config,
    _require_bound_checkpoint,
    _shard_checkpoint,
    read_bundle,
    read_intent,
)
from .models import (
    BUNDLE_FILENAME,
    INTENT_FILENAME,
    GridOperatorError,
    JobBundle,
    JobPaths,
    SubmissionIntent,
)
from .state import submission_lock


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
