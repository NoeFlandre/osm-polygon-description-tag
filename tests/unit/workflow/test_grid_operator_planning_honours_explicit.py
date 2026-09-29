"""Preparing, gating, submitting, reconciling, and collecting one Grid'5000 job."""

import json
import stat
from collections.abc import Sequence
from dataclasses import replace
from datetime import UTC, datetime
from pathlib import Path

import pytest

from osm_polygon_description_tag.dataset.languages.models import (
    LanguageResult,
    LanguageStatus,
)
from osm_polygon_description_tag.dataset.languages.snapshot import (
    SnapshotManifest,
)
from osm_polygon_description_tag.dataset.languages.worker import ProcessingBudget, process_shard
from osm_polygon_description_tag.workflow.grid_operator import (
    GridOperatorError,
    JobBundle,
    JobPaths,
    SubmissionIntent,
    acknowledge_collected_results,
    build_bundle_transfer_argv,
    bundle_for_shard,
    collect_results,
    plan_submission,
    prepare_job,
    prepare_portable_job,
    read_bundle,
    read_intent,
    reconcile_job,
    render_job_script,
    submit_job,
)
from osm_polygon_description_tag.workflow.grid_policy import (
    GRID5000_TIMEZONE,
    MAX_PROCESSING_SECONDS,
    MAX_WALLTIME_SECONDS,
    PolicyDecision,
    PolicyEvidence,
    PolicyVerdict,
)
from osm_polygon_description_tag.workflow.grid_scheduler import (
    CommandResult,
    JobState,
    SubmissionOutcome,
)
from tests.helpers.messages import exactly
from tests.helpers.sentences import REMOTE_SAT_MODEL_PATH, fake_splitter

SHARD = "region.parquet"


REMOTE = {
    "remote_project_dir": "/home/user/project",
    "remote_source_dir": "/scratch/staging/source",
    "remote_run_dir": "/scratch/staging/run",
    "sat_model_path": "/home/user/models/sat-3l-sm/model.safetensors",
}


def _verdict(
    decision: PolicyDecision = PolicyDecision.ALLOWED, *, captured_at: datetime | None = None
) -> PolicyVerdict:
    return PolicyVerdict(
        decision,
        ("test verdict",),
        PolicyEvidence(0, False, True, (), captured_at=captured_at or datetime.now(UTC)),
    )


def _runner(results: list[CommandResult], observed: list[tuple[str, ...]] | None = None) -> object:
    def _run(argv: Sequence[str], timeout: float) -> CommandResult:
        if observed is not None:
            observed.append(tuple(argv))
        return results.pop(0)

    return _run


def test_planning_honours_explicit_limits_and_evaluation_time(
    prepared: tuple[Path, Path, SnapshotManifest],
) -> None:
    """A plan must use the caller's attempt limit, freshness demand, and clock."""
    _, run, snapshot = prepared
    bundle, paths = prepare_job(run, snapshot, SHARD, **REMOTE)
    captured_at = datetime(2026, 9, 9, 21, 0, tzinfo=UTC)
    policy = PolicyVerdict(
        PolicyDecision.ALLOWED,
        ("test verdict",),
        PolicyEvidence(0, False, True, (), captured_at=captured_at),
    )

    # The caller's own instant makes the evidence fresh; the real clock would not.
    fresh, _ = submit_job(
        paths,
        bundle,
        policy=policy,
        allowed_root=run,
        require_fresh_policy=True,
        now=captured_at,
    )
    assert fresh.may_apply

    # A stale instant must block precisely because freshness was demanded.
    stale, _ = submit_job(
        paths,
        bundle,
        policy=policy,
        allowed_root=run,
        require_fresh_policy=True,
        now=datetime(2026, 9, 10, 21, 0, tzinfo=UTC),
    )
    assert not stale.may_apply
    assert stale.blocked_reason is not None

    # The attempt limit is the caller's, and it is reached at the first attempt.
    _write_submitted_intent(paths, bundle)
    limited, _ = submit_job(
        paths,
        bundle,
        policy=policy,
        allowed_root=run,
        max_attempts=1,
        now=captured_at,
    )
    assert not limited.may_apply
    assert limited.blocked_reason is not None
    assert "maximum of 1 attempts" in limited.blocked_reason


def _write_submitted_intent(paths: JobPaths, bundle: JobBundle) -> None:
    """Record a terminal, acknowledged first attempt so a retry may be planned."""
    intent = SubmissionIntent(
        bundle_id=bundle.bundle_id,
        shard=bundle.shard,
        job_name=f"lang-{bundle.bundle_id[:16]}",
        walltime_seconds=MAX_WALLTIME_SECONDS,
        cores=1,
        recorded_at="2026-09-09T21:00:00+00:00",
        job_id=6917617,
        outcome="submitted",
        terminal_state="terminated",
        reconciled_at="2026-09-09T21:05:00+00:00",
        result_acknowledged=True,
    )
    paths.intent.write_text(json.dumps(intent.to_payload()), encoding="utf-8")


def test_an_applied_submission_records_a_utc_intent_and_requests_night_noretry(
    prepared: tuple[Path, Path, SnapshotManifest],
) -> None:
    """Without an injected clock the intent is still timezone-aware UTC."""
    _, run, snapshot = prepared
    bundle, paths = prepare_job(run, snapshot, SHARD, **REMOTE)
    observed: list[tuple[str, ...]] = []

    plan, result = submit_job(
        paths,
        bundle,
        policy=_verdict(),
        allowed_root=run,
        apply=True,
        runner=_runner(  # type: ignore[arg-type]
            [CommandResult((), 0, "OAR_JOB_ID=6917617\n", "")], observed
        ),
    )

    assert result is not None
    assert observed and "night=noretry" in observed[0]
    assert "night=noretry" in plan.argv
    recorded = read_intent(paths.intent)
    assert recorded.recorded_at.endswith("+00:00")
    assert datetime.fromisoformat(recorded.recorded_at).tzinfo is not None
    assert plan.walltime_seconds == MAX_WALLTIME_SECONDS


def test_bundle_transfer_binds_identity_label_and_exact_destination(
    portable_prepared: tuple[Path, Path, Path, SnapshotManifest],
) -> None:
    """The transfer argv is a safety contract, so pin each part of it.

    Only a verified ``PreparedJob`` may be transferred, the payload is checked
    against *this* bundle rather than whatever bundle it happens to contain,
    the remote path is refused under its own label, and the destination keeps
    exactly one trailing separator.
    """
    project, source, run, snapshot = portable_prepared
    prepared = prepare_portable_job(
        run,
        project,
        source,
        snapshot,
        SHARD,
        remote_bundle_dir="/scratch/lang-bundle",
        sat_model_path=REMOTE_SAT_MODEL_PATH,
    )

    with pytest.raises(GridOperatorError) as error:
        build_bundle_transfer_argv(object(), "/scratch/lang-bundle")  # type: ignore[arg-type]
    assert str(error.value) == "prepared bundle must be a PreparedJob"

    foreign = replace(prepared, bundle=replace(prepared.bundle, source_sha256="c" * 64))
    with pytest.raises(GridOperatorError):
        build_bundle_transfer_argv(foreign, "/scratch/lang-bundle")

    with pytest.raises(GridOperatorError) as error:
        build_bundle_transfer_argv(prepared, "/scratch/a b")
    assert str(error.value) == "remote bundle directory must not contain shell metacharacters"

    # Only "/" is stripped, and a root destination stays rooted.
    assert build_bundle_transfer_argv(prepared, "/scratch/bundle/")[-1] == "/scratch/bundle/"
    assert build_bundle_transfer_argv(prepared, "/scratch/bundleX")[-1] == "/scratch/bundleX/"
    assert build_bundle_transfer_argv(prepared, "/")[-1] == "//"


@pytest.mark.parametrize(
    ("overrides", "message"),
    [
        # Each message names the offending input, so an operator can tell which
        # path was refused; assert the full text, not just the failure kind.
        (
            {"remote_project_dir": "relative/path"},
            "remote project directory must be an absolute path",
        ),
        (
            {"remote_project_dir": ""},
            "remote project directory must be a non-empty string",
        ),
        (
            {"remote_project_dir": "/scratch/../evil"},
            "remote project directory must not contain traversal components",
        ),
        (
            {"remote_run_dir": "/scratch/a b"},
            "remote run directory must not contain shell metacharacters",
        ),
        (
            {"remote_run_dir": "relative/run"},
            "remote run directory must be an absolute path",
        ),
        (
            {"remote_source_dir": "/scratch/$(evil)"},
            "remote source directory must not contain shell metacharacters",
        ),
        ({"remote_source_dir": ""}, "remote source directory must be a non-empty string"),
        (
            {"remote_source_dir": "/scratch/../evil"},
            "remote source directory must not contain traversal components",
        ),
        (
            {"remote_bundle_dir": "/scratch/../bundle"},
            "remote bundle directory must not contain traversal components",
        ),
        (
            {"remote_bundle_dir": "/scratch/bundle;rm"},
            "remote bundle directory must not contain shell metacharacters",
        ),
        (
            {"glotlid_model_path": "models/model_v3.bin"},
            "remote GlotLID model path must be an absolute path",
        ),
        (
            {"glotlid_model_path": "/models/../model_v3.bin"},
            "remote GlotLID model path must not contain traversal components",
        ),
        ({"processing_seconds": 0}, "processing budget must be between"),
        ({"processing_seconds": MAX_PROCESSING_SECONDS + 1}, "processing budget must be between"),
        ({"batch_size": 0}, "batch size must be a positive integer"),
    ],
)
def test_the_job_script_rejects_unsafe_parameters(
    prepared: tuple[Path, Path, SnapshotManifest],
    overrides: dict[str, object],
    message: str,
) -> None:
    _, _, snapshot = prepared

    with pytest.raises(GridOperatorError) as error:
        render_job_script(bundle_for_shard(snapshot, SHARD), **{**REMOTE, **overrides})

    assert message in str(error.value)


def test_preparing_a_job_writes_an_executable_script_and_bundle(
    prepared: tuple[Path, Path, SnapshotManifest],
) -> None:
    _, run, snapshot = prepared

    bundle, paths = prepare_job(run, snapshot, SHARD, **REMOTE)

    assert paths.bundle.is_file()
    assert paths.script.is_file()
    assert stat.S_IMODE(paths.script.stat().st_mode) == 0o700
    assert read_bundle(paths.bundle) == bundle
    assert not paths.intent.exists()


def test_preparing_a_job_twice_is_idempotent(
    prepared: tuple[Path, Path, SnapshotManifest],
) -> None:
    _, run, snapshot = prepared

    first, _ = prepare_job(run, snapshot, SHARD, **REMOTE)
    second, _ = prepare_job(run, snapshot, SHARD, **REMOTE)

    assert first == second


def test_repreparing_a_bundle_rejects_changed_limits_or_remote_paths(
    prepared: tuple[Path, Path, SnapshotManifest],
) -> None:
    _, run, snapshot = prepared
    _, paths = prepare_job(run, snapshot, SHARD, **REMOTE)
    original_config = paths.config.read_bytes()
    original_script = paths.script.read_bytes()

    with pytest.raises(
        GridOperatorError,
        match=exactly("prepared job config is immutable; restage with a new job bundle"),
    ):
        prepare_job(
            run,
            snapshot,
            SHARD,
            **REMOTE,
            walltime_seconds=MAX_WALLTIME_SECONDS - 1,
        )
    with pytest.raises(
        GridOperatorError,
        match=exactly("prepared job config is immutable; restage with a new job bundle"),
    ):
        prepare_job(
            run,
            snapshot,
            SHARD,
            remote_project_dir="/home/other-project",
            remote_source_dir=REMOTE["remote_source_dir"],
            remote_run_dir=REMOTE["remote_run_dir"],
            sat_model_path=REMOTE_SAT_MODEL_PATH,
        )

    assert paths.config.read_bytes() == original_config
    assert paths.script.read_bytes() == original_script


def test_a_conflicting_bundle_in_a_job_directory_is_rejected(
    prepared: tuple[Path, Path, SnapshotManifest],
) -> None:
    _, run, snapshot = prepared
    bundle, paths = prepare_job(run, snapshot, SHARD, **REMOTE)
    payload = bundle.to_payload()
    payload["code_fingerprint"] = "c" * 64
    foreign = JobBundle(
        **{k: v for k, v in payload.items() if k not in {"bundle_id", "bundle_schema_version"}}
    )
    paths.bundle.write_text(json.dumps(foreign.to_payload()), encoding="utf-8")

    with pytest.raises(
        GridOperatorError,
        match=exactly("a different bundle is already prepared in this job directory"),
    ):
        prepare_job(run, snapshot, SHARD, **REMOTE)


def test_planning_contacts_no_scheduler_and_reports_the_argv(
    prepared: tuple[Path, Path, SnapshotManifest],
) -> None:
    _, run, snapshot = prepared
    bundle, paths = prepare_job(run, snapshot, SHARD, **REMOTE)

    plan = plan_submission(paths, bundle, policy=_verdict(), allowed_root=run)

    assert plan.may_apply
    assert plan.cores == 1
    assert plan.walltime_seconds == MAX_WALLTIME_SECONDS
    assert plan.argv[0] == "oarsub"
    assert "core=1,walltime=0:30:00" in plan.argv
    assert "night=noretry" in plan.argv
    assert not paths.intent.exists()
    assert json.loads(json.dumps(plan.to_payload()))["may_apply"] is True


def test_submit_job_defaults_are_fail_safe(
    prepared: tuple[Path, Path, SnapshotManifest],
) -> None:
    """Every default on the submission entry point must be the safe one.

    ``apply`` defaulting to ``True`` would submit a real job from a plain call,
    ``night_noretry`` defaulting to ``False`` would let a postponed night job
    be retried silently, and ``require_fresh_policy`` defaulting to ``True``
    would make planning demand live evidence it never gathered.
    """
    _, run, snapshot = prepared
    bundle, paths = prepare_job(run, snapshot, SHARD, **REMOTE)
    observed: list[tuple[str, ...]] = []
    unknown_freshness = PolicyVerdict(
        PolicyDecision.ALLOWED,
        ("test verdict",),
        PolicyEvidence(0, False, True, (), captured_at=None),
    )

    plan, result = submit_job(
        paths,
        bundle,
        policy=unknown_freshness,
        allowed_root=run,
        runner=_runner([], observed),  # type: ignore[arg-type]
    )

    assert result is None
    assert observed == []
    assert not paths.intent.exists()
    assert "night=noretry" in plan.argv
    assert plan.may_apply


def test_a_blocked_policy_prevents_applying(
    prepared: tuple[Path, Path, SnapshotManifest],
) -> None:
    _, run, snapshot = prepared
    bundle, paths = prepare_job(run, snapshot, SHARD, **REMOTE)

    for decision in (PolicyDecision.BLOCKED, PolicyDecision.UNKNOWN):
        plan = plan_submission(paths, bundle, policy=_verdict(decision), allowed_root=run)
        assert not plan.may_apply


def test_an_oversized_walltime_is_refused(
    prepared: tuple[Path, Path, SnapshotManifest],
) -> None:
    _, run, snapshot = prepared
    bundle, paths = prepare_job(run, snapshot, SHARD, **REMOTE)

    with pytest.raises(GridOperatorError, match="must not exceed 1800 seconds"):
        plan_submission(
            paths,
            bundle,
            policy=_verdict(),
            allowed_root=run,
            walltime_seconds=MAX_WALLTIME_SECONDS + 1,
        )


@pytest.mark.parametrize(
    "captured_at",
    [None, datetime(2026, 9, 5, 21, 0, tzinfo=UTC)],
)
def test_direct_apply_refuses_missing_or_stale_policy_evidence(
    prepared: tuple[Path, Path, SnapshotManifest], captured_at: datetime | None
) -> None:
    _, run, snapshot = prepared
    bundle, paths = prepare_job(run, snapshot, SHARD, **REMOTE)
    policy = PolicyVerdict(
        PolicyDecision.ALLOWED,
        ("test verdict",),
        PolicyEvidence(0, False, True, (), captured_at=captured_at),
    )
    observed: list[tuple[str, ...]] = []

    plan, result = submit_job(
        paths,
        bundle,
        policy=policy,
        allowed_root=run,
        apply=True,
        runner=_runner([], observed),  # type: ignore[arg-type]
        now=datetime(2026, 9, 5, 22, 0, tzinfo=UTC),
    )

    assert result is None
    assert not plan.may_apply
    assert plan.blocked_reason is not None
    assert any(word in plan.blocked_reason for word in ("fresh", "stale"))
    assert observed == []
    assert not paths.intent.exists()


def test_planning_rejects_a_walltime_different_from_prepared_job_config(
    prepared: tuple[Path, Path, SnapshotManifest],
) -> None:
    _, run, snapshot = prepared
    bundle, paths = prepare_job(
        run,
        snapshot,
        SHARD,
        **REMOTE,
        walltime_seconds=MAX_WALLTIME_SECONDS - 1,
    )

    with pytest.raises(
        GridOperatorError, match=exactly("submission walltime differs from prepared job config")
    ):
        plan_submission(paths, bundle, policy=_verdict(), allowed_root=run)


def test_without_the_apply_gate_nothing_is_submitted(
    prepared: tuple[Path, Path, SnapshotManifest],
) -> None:
    _, run, snapshot = prepared
    bundle, paths = prepare_job(run, snapshot, SHARD, **REMOTE)
    observed: list[tuple[str, ...]] = []

    plan, result = submit_job(
        paths,
        bundle,
        policy=_verdict(),
        allowed_root=run,
        runner=_runner([], observed),  # type: ignore[arg-type]
    )

    assert result is None
    assert plan.may_apply
    assert observed == []
    assert not paths.intent.exists()


def test_a_blocked_policy_is_not_submitted_even_with_the_apply_gate(
    prepared: tuple[Path, Path, SnapshotManifest],
) -> None:
    _, run, snapshot = prepared
    bundle, paths = prepare_job(run, snapshot, SHARD, **REMOTE)
    observed: list[tuple[str, ...]] = []

    _, result = submit_job(
        paths,
        bundle,
        policy=_verdict(PolicyDecision.BLOCKED),
        allowed_root=run,
        apply=True,
        runner=_runner([], observed),  # type: ignore[arg-type]
    )

    assert result is None
    assert observed == []
    assert not paths.intent.exists()


def test_the_intent_is_durable_before_oarsub_is_called(
    prepared: tuple[Path, Path, SnapshotManifest],
) -> None:
    _, run, snapshot = prepared
    bundle, paths = prepare_job(run, snapshot, SHARD, **REMOTE)
    seen_intent: list[bool] = []

    def _run(argv: Sequence[str], timeout: float) -> CommandResult:
        seen_intent.append(paths.intent.is_file())
        return CommandResult(tuple(argv), 0, "OAR_JOB_ID=1234\n", "")

    _, result = submit_job(
        paths,
        bundle,
        allowed_root=run,
        apply=True,
        runner=_run,
        policy=_verdict(captured_at=datetime(2026, 9, 5, 21, 59, tzinfo=GRID5000_TIMEZONE)),
        now=datetime(2026, 9, 5, 22, 0, tzinfo=GRID5000_TIMEZONE),
    )

    assert seen_intent == [True]
    assert result is not None
    assert result.outcome is SubmissionOutcome.SUBMITTED
    intent = read_intent(paths.intent)
    assert intent.job_id == 1234
    assert intent.outcome == "submitted"
    assert not intent.is_unresolved
    assert intent.recorded_at.startswith("2026-09-05T22:00")


def test_an_ambiguous_submission_leaves_an_unresolved_intent(
    prepared: tuple[Path, Path, SnapshotManifest],
) -> None:
    _, run, snapshot = prepared
    bundle, paths = prepare_job(run, snapshot, SHARD, **REMOTE)

    _, result = submit_job(
        paths,
        bundle,
        policy=_verdict(),
        allowed_root=run,
        apply=True,
        runner=_runner([CommandResult((), -1, "", "", True)]),  # type: ignore[arg-type]
    )

    assert result is not None
    assert result.outcome is SubmissionOutcome.AMBIGUOUS
    intent = read_intent(paths.intent)
    assert intent.job_id is None
    assert intent.is_unresolved


def test_an_unresolved_intent_blocks_any_further_submission(
    prepared: tuple[Path, Path, SnapshotManifest],
) -> None:
    _, run, snapshot = prepared
    bundle, paths = prepare_job(run, snapshot, SHARD, **REMOTE)
    submit_job(
        paths,
        bundle,
        policy=_verdict(),
        allowed_root=run,
        apply=True,
        runner=_runner([CommandResult((), -1, "", "", True)]),  # type: ignore[arg-type]
    )
    observed: list[tuple[str, ...]] = []

    plan, result = submit_job(
        paths,
        bundle,
        policy=_verdict(),
        allowed_root=run,
        apply=True,
        runner=_runner([], observed),  # type: ignore[arg-type]
    )

    assert result is None
    assert not plan.may_apply
    assert plan.blocked_reason is not None
    assert "unresolved intent" in plan.blocked_reason
    assert observed == []


def test_only_a_terminal_reconciled_and_acknowledged_paused_shard_can_retry(
    prepared: tuple[Path, Path, SnapshotManifest],
) -> None:
    source, run, snapshot = prepared
    bundle, paths = prepare_job(run, snapshot, SHARD, **REMOTE)
    submit_job(
        paths,
        bundle,
        policy=_verdict(),
        allowed_root=run,
        apply=True,
        runner=_runner([CommandResult((), 0, "OAR_JOB_ID=21\n", "")]),  # type: ignore[arg-type]
    )

    reconciled = reconcile_job(
        paths,
        apply=True,
        runner=_runner([CommandResult((), 0, "21: Terminated\n", "")]),  # type: ignore[arg-type]
    )
    assert reconciled.state is JobState.TERMINATED
    assert read_intent(paths.intent).terminal_state == "terminated"

    process_shard(
        run,
        source,
        SHARD,
        detector=lambda text: LanguageResult(
            "eng", 0.9, 0.1, 0.8, LanguageStatus.DETECTED, "detected"
        ),
        splitter=fake_splitter(),
        snapshot=snapshot,
        batch_size=2,
    )
    report = collect_results(run, SHARD)
    assert report.is_complete

    acknowledgment = acknowledge_collected_results(paths, report)
    assert acknowledgment.result_complete
    blocked = plan_submission(paths, bundle, policy=_verdict(), allowed_root=run)
    assert not blocked.may_apply
    assert blocked.blocked_reason is not None
    assert "complete results" in blocked.blocked_reason


def test_a_paused_result_acknowledgment_opens_one_bounded_same_shard_attempt(
    prepared: tuple[Path, Path, SnapshotManifest],
) -> None:
    source, run, snapshot = prepared
    bundle, paths = prepare_job(run, snapshot, SHARD, **REMOTE)
    submit_job(
        paths,
        bundle,
        policy=_verdict(),
        allowed_root=run,
        apply=True,
        runner=_runner([CommandResult((), 0, "OAR_JOB_ID=31\n", "")]),  # type: ignore[arg-type]
    )
    reconcile_job(
        paths,
        apply=True,
        runner=_runner([CommandResult((), 0, "31: Terminated\n", "")]),  # type: ignore[arg-type]
    )

    # A valid paused checkpoint with no committed rows is a real resumable state.
    process_shard(
        run,
        source,
        SHARD,
        detector=lambda text: LanguageResult(
            "eng", 0.9, 0.1, 0.8, LanguageStatus.DETECTED, "detected"
        ),
        splitter=fake_splitter(),
        snapshot=snapshot,
        batch_size=2,
        budget=ProcessingBudget(0.000001),
    )
    report = collect_results(run, SHARD)
    assert not report.is_complete
    assert not report.issues
    acknowledge_collected_results(paths, report)

    retry_plan = plan_submission(paths, bundle, policy=_verdict(), allowed_root=run)
    assert retry_plan.may_apply
    assert retry_plan.attempt == 2

    _, retry_result = submit_job(
        paths,
        bundle,
        policy=_verdict(),
        allowed_root=run,
        apply=True,
        runner=_runner([CommandResult((), 0, "OAR_JOB_ID=32\n", "")]),  # type: ignore[arg-type]
    )
    assert retry_result is not None
    reconcile_job(
        paths,
        apply=True,
        runner=_runner([CommandResult((), 0, "32: Terminated\n", "")]),  # type: ignore[arg-type]
    )
    acknowledge_collected_results(paths, report)

    exhausted = plan_submission(
        paths,
        bundle,
        policy=_verdict(),
        allowed_root=run,
        max_attempts=2,
    )
    assert not exhausted.may_apply
    assert exhausted.blocked_reason is not None
    assert "maximum" in exhausted.blocked_reason

    continuation = plan_submission(paths, bundle, policy=_verdict(), allowed_root=run)
    assert continuation.may_apply
    assert continuation.attempt == 3


def test_collected_acknowledgment_requires_terminal_reconciliation(
    prepared: tuple[Path, Path, SnapshotManifest],
) -> None:
    source, run, snapshot = prepared
    bundle, paths = prepare_job(run, snapshot, SHARD, **REMOTE)
    submit_job(
        paths,
        bundle,
        policy=_verdict(),
        allowed_root=run,
        apply=True,
        runner=_runner([CommandResult((), 0, "OAR_JOB_ID=30\n", "")]),  # type: ignore[arg-type]
    )
    process_shard(
        run,
        source,
        SHARD,
        detector=lambda text: LanguageResult(
            "eng", 0.9, 0.1, 0.8, LanguageStatus.DETECTED, "detected"
        ),
        splitter=fake_splitter(),
        snapshot=snapshot,
        batch_size=2,
        budget=ProcessingBudget(0.000001),
    )

    with pytest.raises(
        GridOperatorError, match=exactly("collected results require a terminal scheduler state")
    ):
        acknowledge_collected_results(paths, collect_results(run, SHARD))
