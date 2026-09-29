"""Preparing, gating, submitting, reconciling, and collecting one Grid'5000 job."""

import json
from collections.abc import Sequence
from datetime import UTC, datetime
from pathlib import Path

import pytest
from shapely.geometry import Polygon

from osm_polygon_description_tag.dataset.languages.checkpoint import (
    ShardCheckpoint,
    ShardStatus,
    shard_paths,
    write_checkpoint,
)
from osm_polygon_description_tag.dataset.languages.models import (
    LanguageResult,
    LanguageStatus,
    cascade_model_identity,
)
from osm_polygon_description_tag.dataset.languages.snapshot import (
    SnapshotManifest,
    prepare_snapshot,
)
from osm_polygon_description_tag.dataset.languages.worker import process_shard
from osm_polygon_description_tag.dataset.storage import write_geoparquet
from osm_polygon_description_tag.workflow import grid_operator
from osm_polygon_description_tag.workflow.grid_operator import (
    GridOperatorError,
    bundle_for_shard,
    collect_results,
    gather_policy_evidence,
    job_paths,
    plan_submission,
    prepare_job,
    prepare_portable_job,
    read_intent,
    reconcile_job,
    resolve_job_name,
    submit_job,
)
from osm_polygon_description_tag.workflow.grid_policy import (
    MAX_WALLTIME_SECONDS,
    PolicyDecision,
    PolicyEvidence,
    PolicyVerdict,
)
from osm_polygon_description_tag.workflow.grid_scheduler import (
    CommandResult,
    JobState,
)
from tests.conftest import make_record_dict
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


def _two_shard_prepared(tmp_path: Path) -> tuple[Path, SnapshotManifest, dict[str, Path]]:
    source = tmp_path / "source"
    source.mkdir(parents=True)
    for shard in (SHARD, "other.parquet"):
        write_geoparquet(
            (
                make_record_dict(
                    Polygon([(0, 0), (0, 1), (1, 1), (1, 0)]),
                    {"description": f"A synthetic description {index}"},
                    osm_id=index + 1,
                )
                for index in range(2)
            ),
            source / shard,
            batch_size=1,
        )
    run = tmp_path / "run"
    snapshot = prepare_snapshot(source, run, code_fingerprint="a" * 64, lock_fingerprint="b" * 64)
    return run, snapshot, {"source": source, "run": run}


def _runner(results: list[CommandResult], observed: list[tuple[str, ...]] | None = None) -> object:
    def _run(argv: Sequence[str], timeout: float) -> CommandResult:
        if observed is not None:
            observed.append(tuple(argv))
        return results.pop(0)

    return _run


def test_collection_rejects_a_complete_checkpoint_without_contiguous_parts(
    prepared: tuple[Path, Path, SnapshotManifest],
) -> None:
    _, run, snapshot = prepared
    paths = shard_paths(run, SHARD)
    checkpoint = ShardCheckpoint(
        snapshot_id=snapshot.snapshot_id,
        model_config_fingerprint=snapshot.model_config_fingerprint,
        shard=SHARD,
        batch_size=2,
        input_row_count=4,
        input_cursor=0,
        annotation_count=0,
        completed_parts=(),
        status=ShardStatus.PAUSED,
    )
    write_checkpoint(paths.checkpoint, checkpoint)
    payload = json.loads(paths.checkpoint.read_text(encoding="utf-8"))
    paths.checkpoint.write_text(
        json.dumps({**payload, "input_cursor": 4, "status": "complete"}), encoding="utf-8"
    )

    report = collect_results(run, SHARD)

    assert not report.is_complete
    assert any("checkpoint parts do not cover" in issue for issue in report.shards[0].issues)


def test_run_wide_active_intent_blocks_a_different_shard(tmp_path: Path) -> None:
    run, snapshot, roots = _two_shard_prepared(tmp_path)
    first, first_paths = prepare_job(run, snapshot, SHARD, **REMOTE)
    second, second_paths = prepare_job(run, snapshot, "other.parquet", **REMOTE)
    submit_job(
        first_paths,
        first,
        policy=_verdict(),
        allowed_root=run,
        apply=True,
        runner=_runner([CommandResult((), 0, "OAR_JOB_ID=41\n", "")]),  # type: ignore[arg-type]
    )

    plan = plan_submission(second_paths, second, policy=_verdict(), allowed_root=run)

    assert not plan.may_apply
    assert plan.blocked_reason is not None
    assert "another shard" in plan.blocked_reason
    assert roots["source"].is_dir()


def test_submission_lock_serializes_the_scheduler_call(tmp_path: Path) -> None:
    run, snapshot, _ = _two_shard_prepared(tmp_path)
    first, first_paths = prepare_job(run, snapshot, SHARD, **REMOTE)
    second, second_paths = prepare_job(run, snapshot, "other.parquet", **REMOTE)
    observed: list[tuple[str, ...]] = []

    def runner(argv: Sequence[str], timeout: float) -> CommandResult:
        with pytest.raises(GridOperatorError, match="submission lock"):
            submit_job(
                second_paths,
                second,
                policy=_verdict(),
                allowed_root=run,
                apply=True,
                runner=_runner([], observed),  # type: ignore[arg-type]
            )
        return CommandResult(tuple(argv), 0, "OAR_JOB_ID=42\n", "")

    _, result = submit_job(
        first_paths,
        first,
        policy=_verdict(),
        allowed_root=run,
        apply=True,
        runner=runner,
    )

    assert result is not None
    assert result.job_id == 42
    assert observed == []


def test_an_existing_job_blocks_a_second_submission(
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
        runner=_runner([CommandResult((), 0, "OAR_JOB_ID=7\n", "")]),  # type: ignore[arg-type]
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
    assert plan.blocked_reason is not None
    assert "job 7 was already submitted" in plan.blocked_reason
    assert observed == []


def test_a_rejected_submission_does_not_block_a_later_attempt(
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
        runner=_runner([CommandResult((), 2, "", "refused")]),  # type: ignore[arg-type]
    )

    plan, result = submit_job(
        paths,
        bundle,
        policy=_verdict(),
        allowed_root=run,
        apply=True,
        runner=_runner([CommandResult((), 0, "OAR_JOB_ID=8\n", "")]),  # type: ignore[arg-type]
    )

    assert plan.blocked_reason is None
    assert result is not None
    assert result.job_id == 8


def test_rejected_submissions_still_count_toward_an_explicit_attempt_limit(
    prepared: tuple[Path, Path, SnapshotManifest],
) -> None:
    _, run, snapshot = prepared
    bundle, paths = prepare_job(run, snapshot, SHARD, **REMOTE)
    calls: list[tuple[str, ...]] = []

    def reject(argv: Sequence[str], timeout: float) -> CommandResult:
        calls.append(tuple(argv))
        return CommandResult(tuple(argv), 2, "", "refused")

    for _ in range(2):
        plan, result = submit_job(
            paths,
            bundle,
            policy=_verdict(),
            allowed_root=run,
            apply=True,
            runner=reject,
            max_attempts=1,
        )

    assert len(calls) == 1
    assert result is None
    assert plan.blocked_reason == "maximum of 1 attempts has been reached for this shard"
    assert read_intent(paths.intent).attempt == 1


def test_reconciliation_reports_a_missing_intent(
    prepared: tuple[Path, Path, SnapshotManifest],
) -> None:
    _, run, snapshot = prepared
    bundle = bundle_for_shard(snapshot, SHARD)

    reconciliation = reconcile_job(job_paths(run, bundle))

    assert reconciliation.intent is None
    assert not reconciliation.needs_operator_attention
    assert "no submission intent" in reconciliation.detail


def test_reconciliation_flags_an_unresolved_intent_for_an_operator(
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

    reconciliation = reconcile_job(paths)

    assert reconciliation.needs_operator_attention
    assert "query the scheduler by job name" in reconciliation.detail
    assert json.loads(json.dumps(reconciliation.to_payload()))["state"] == "unknown"


def test_reconciliation_resolver_binds_a_job_name_before_querying_state(
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

    reconciliation = reconcile_job(
        paths,
        apply=True,
        resolve_job_name=lambda name: 77,
        runner=_runner([CommandResult((), 0, "77: Terminated\n", "")]),  # type: ignore[arg-type]
    )

    assert reconciliation.state is JobState.TERMINATED
    assert read_intent(paths.intent).job_id == 77
    assert read_intent(paths.intent).terminal_state == "terminated"


def test_reconciliation_rejects_a_non_callable_job_name_resolver(
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

    with pytest.raises(GridOperatorError, match=exactly("job-name resolver must be callable")):
        reconcile_job(  # type: ignore[arg-type]
            paths,
            apply=True,
            resolve_job_name=object(),
        )


def test_policy_evidence_gatherer_includes_account_wide_scheduler_output() -> None:
    observed: list[tuple[str, ...]] = []
    runner = _runner(
        [
            CommandResult((), 0, "policy-json", ""),
            CommandResult((), 0, "quota-text", ""),
            CommandResult((), 0, '{"123":{"state":"Running"}}', ""),
        ],
        observed,
    )

    evidence = gather_policy_evidence("nancy", runner=runner)  # type: ignore[arg-type]

    assert evidence == ("policy-json", "quota-text", '{"123":{"state":"Running"}}')
    assert observed[-1] == ("oarstat", "-u", "-J")


def test_production_job_name_resolver_uses_account_json_without_resubmitting() -> None:
    observed: list[tuple[str, ...]] = []

    def runner(argv: Sequence[str], timeout: float) -> CommandResult:
        observed.append(tuple(argv))
        return CommandResult(
            tuple(argv),
            0,
            '{"123":{"name":"lang-target","state":"Terminated"}}',
            "",
        )

    assert resolve_job_name("lang-target", runner=runner) == 123
    assert observed == [("oarstat", "-u", "-J")]


@pytest.mark.parametrize(
    ("account", "expected"),
    [
        pytest.param(CommandResult((), 0, "", ""), "{}", id="empty-success-is-no-jobs"),
        pytest.param(CommandResult((), 1, "", "oarstat failed"), None, id="failed-is-unknown"),
        pytest.param(CommandResult((), 0, "", "", timed_out=True), None, id="timeout-is-unknown"),
    ],
)
def test_policy_evidence_treats_only_a_successful_empty_account_listing_as_no_jobs(
    account: CommandResult, expected: str | None
) -> None:
    observed: list[tuple[str, ...]] = []
    runner = _runner(
        [CommandResult((), 0, "policy", ""), CommandResult((), 0, "quota", ""), account],
        observed,
    )

    assert gather_policy_evidence("nancy", runner=runner) == (  # type: ignore[arg-type]
        "policy",
        "quota",
        expected,
    )
    assert observed == [
        ("usagepolicycheck", "-t", "--sites", "nancy", "--json"),
        ("quota", "-p", "-w"),
        ("oarstat", "-u", "-J"),
    ]


def test_job_name_resolver_treats_successful_empty_account_output_as_no_match() -> None:
    observed: list[tuple[str, ...]] = []
    runner = _runner([CommandResult((), 0, "", "")], observed)

    assert resolve_job_name("lang-target", runner=runner) is None  # type: ignore[arg-type]
    assert observed == [("oarstat", "-u", "-J")]


def test_job_name_resolver_keeps_successful_whitespace_output_strict() -> None:
    runner = _runner([CommandResult((), 0, " \n", "")])

    with pytest.raises(
        GridOperatorError,
        match=exactly(
            "cannot resolve account job by name: account-wide oarstat output must be valid JSON"
        ),
    ):
        resolve_job_name("lang-target", runner=runner)  # type: ignore[arg-type]


def test_reconciliation_queries_the_scheduler_only_behind_the_apply_gate(
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
        runner=_runner([CommandResult((), 0, "OAR_JOB_ID=11\n", "")]),  # type: ignore[arg-type]
    )
    observed: list[tuple[str, ...]] = []

    dry = reconcile_job(paths, runner=_runner([], observed))  # type: ignore[arg-type]
    assert observed == []
    assert "pass the apply gate" in dry.detail

    live = reconcile_job(
        paths,
        apply=True,
        runner=_runner([CommandResult((), 0, "11: Running\n", "")], observed),  # type: ignore[arg-type]
    )

    assert observed == [("oarstat", "-j", "11", "-s")]
    assert live.state is JobState.ACTIVE
    assert not live.needs_operator_attention


def test_a_terminated_job_needs_no_further_attention(
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
        runner=_runner([CommandResult((), 0, "OAR_JOB_ID=12\n", "")]),  # type: ignore[arg-type]
    )

    reconciliation = reconcile_job(
        paths,
        apply=True,
        runner=_runner([CommandResult((), 0, "12: Terminated\n", "")]),  # type: ignore[arg-type]
    )

    assert reconciliation.state is JobState.TERMINATED
    assert not reconciliation.needs_operator_attention


def test_an_unreadable_intent_is_reported(
    prepared: tuple[Path, Path, SnapshotManifest],
) -> None:
    _, run, snapshot = prepared
    _, paths = prepare_job(run, snapshot, SHARD, **REMOTE)
    paths.intent.write_text("{not-json", encoding="utf-8")

    with pytest.raises(GridOperatorError, match="cannot read intent"):
        reconcile_job(paths)


@pytest.mark.parametrize(
    ("mutation", "message"),
    [
        ({"intent_schema_version": 9}, "unsupported intent schema version"),
        ({"job_id": "7"}, "intent field job_id must be an integer or null"),
        ({"outcome": 7}, "intent field outcome must be a string or null"),
        ({"detail": []}, "intent field detail must be a string or null"),
        ({"shard": None}, "intent field shard must be a string"),
    ],
)
def test_a_malformed_intent_payload_is_rejected(
    prepared: tuple[Path, Path, SnapshotManifest],
    mutation: dict[str, object],
    message: str,
) -> None:
    _, run, snapshot = prepared
    bundle, paths = prepare_job(run, snapshot, SHARD, **REMOTE)
    submit_job(
        paths,
        bundle,
        policy=_verdict(),
        allowed_root=run,
        apply=True,
        runner=_runner([CommandResult((), 0, "OAR_JOB_ID=13\n", "")]),  # type: ignore[arg-type]
    )
    payload = json.loads(paths.intent.read_text(encoding="utf-8"))
    paths.intent.write_text(json.dumps({**payload, **mutation}), encoding="utf-8")

    with pytest.raises(GridOperatorError, match=exactly(message)):
        read_intent(paths.intent)


def test_collecting_results_validates_what_the_job_produced(
    prepared: tuple[Path, Path, SnapshotManifest],
) -> None:
    source, run, snapshot = prepared

    incomplete = collect_results(run, SHARD)
    assert not incomplete.is_complete

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
    assert report.annotation_count == 4


@pytest.mark.parametrize("site", ["", "nancy;rm", "../nancy", 7])
def test_gathering_policy_evidence_rejects_an_unsafe_site(site: object) -> None:
    from osm_polygon_description_tag.workflow.grid_operator import gather_policy_outputs

    with pytest.raises(GridOperatorError, match=exactly("site must be a simple alphanumeric name")):
        gather_policy_outputs(site)  # type: ignore[arg-type]


def test_gathering_policy_evidence_captures_both_commands() -> None:
    from osm_polygon_description_tag.workflow.grid_operator import gather_policy_outputs

    observed: list[tuple[str, ...]] = []
    runner = _runner(
        [CommandResult((), 0, "policy-json", ""), CommandResult((), 0, "quota-text", "")], observed
    )

    usage, quota = gather_policy_outputs("nancy", runner=runner)  # type: ignore[arg-type]

    assert usage == "policy-json"
    assert quota == "quota-text"
    assert observed == [
        ("usagepolicycheck", "-t", "--sites", "nancy", "--json"),
        ("quota", "-p", "-w"),
    ]


def test_evidence_that_cannot_be_captured_is_unknown_rather_than_clear() -> None:
    from osm_polygon_description_tag.workflow.grid_operator import gather_policy_outputs

    failing = _runner([CommandResult((), 1, "", "nope"), CommandResult((), -1, "", "", True)])

    assert gather_policy_outputs("nancy", runner=failing) == (None, None)  # type: ignore[arg-type]


def test_a_missing_preflight_executable_is_unknown_rather_than_clear() -> None:
    from osm_polygon_description_tag.workflow.grid_operator import gather_policy_outputs
    from osm_polygon_description_tag.workflow.grid_scheduler import SchedulerError

    def _absent(argv: Sequence[str], timeout: float) -> CommandResult:
        raise SchedulerError(f"scheduler executable is not available: {argv[0]}")

    assert gather_policy_outputs("nancy", runner=_absent) == (None, None)


def test_cascade_job_script_passes_the_pinned_glotlid_path(
    prepared: tuple[Path, Path, SnapshotManifest],
    tmp_path: Path,
) -> None:
    source, _, original = prepared
    run = tmp_path / "cascade-run"
    snapshot = prepare_snapshot(
        source,
        run,
        code_fingerprint=original.code_fingerprint,
        lock_fingerprint=original.lock_fingerprint,
        model_identity=cascade_model_identity(original.model_identity.policy),
    )

    _, paths = prepare_job(
        run,
        snapshot,
        SHARD,
        **REMOTE,
        glotlid_model_path="/home/user/models/glotlid-v3/model_v3.bin",
    )
    script = paths.script.read_text(encoding="utf-8")

    assert 'export PATH="${PATH}:$HOME/.local/bin"' in script
    assert "--glotlid-model-path /home/user/models/glotlid-v3/model_v3.bin" in script


def test_cascade_job_requires_a_pinned_glotlid_path(
    prepared: tuple[Path, Path, SnapshotManifest],
    tmp_path: Path,
) -> None:
    source, _, original = prepared
    run = tmp_path / "cascade-run"
    snapshot = prepare_snapshot(
        source,
        run,
        code_fingerprint=original.code_fingerprint,
        lock_fingerprint=original.lock_fingerprint,
        model_identity=cascade_model_identity(original.model_identity.policy),
    )

    with pytest.raises(
        GridOperatorError, match=exactly("cascade job requires a GlotLID model path")
    ):
        prepare_job(run, snapshot, SHARD, **REMOTE)


def test_the_job_name_is_the_bundle_id_truncated_to_sixteen_characters(
    prepared: tuple[Path, Path, SnapshotManifest],
) -> None:
    """The name is how a submission is later resolved, so its width is a contract."""
    _, run, snapshot = prepared
    bundle, paths = prepare_job(run, snapshot, SHARD, **REMOTE)

    request = grid_operator._submission_request(bundle, MAX_WALLTIME_SECONDS, True, paths)

    assert request.name == f"lang-{bundle.bundle_id[:16]}"
    assert len(request.name) == len("lang-") + 16
    assert request.cores == 1


@pytest.mark.parametrize("batch_size", [0, -1, True, 1.0, "512", None])
def test_a_batch_size_that_is_not_a_positive_integer_is_refused_exactly(
    batch_size: object,
) -> None:
    with pytest.raises(GridOperatorError) as caught:
        grid_operator._validate_batch_size(batch_size)

    assert str(caught.value) == "batch size must be a positive integer"


def test_the_smallest_positive_batch_size_is_accepted() -> None:
    assert grid_operator._validate_batch_size(1) == 1


def test_payload_files_are_listed_in_posix_relative_path_order(tmp_path: Path) -> None:
    """The stage manifest binds files in this order, so it must be deterministic."""
    root = tmp_path / "payload"
    for relative in ("b/z.txt", "a/y.txt", "a/b.txt", "top.txt"):
        target = root / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text("x", encoding="utf-8")

    listed = [path.relative_to(root).as_posix() for path in grid_operator._payload_files(root)]

    assert listed == sorted(listed)
    assert listed == ["a/b.txt", "a/y.txt", "b/z.txt", "top.txt"]


def test_a_resume_payload_directory_is_named_by_sixteen_fingerprint_characters(
    portable_prepared: tuple[Path, Path, Path, SnapshotManifest],
) -> None:
    project, source, run, snapshot = portable_prepared
    prepared_job = prepare_portable_job(
        run,
        project,
        source,
        snapshot,
        SHARD,
        remote_bundle_dir="/home/user/bundle",
        sat_model_path=REMOTE_SAT_MODEL_PATH,
    )

    name = prepared_job.payload_root.name
    assert name.startswith("payload")
    if name != "payload":
        assert len(name) == len("payload-") + 16


def test_an_applied_submission_requests_the_queue_the_operator_named(
    prepared: tuple[Path, Path, SnapshotManifest],
) -> None:
    """The queue has to survive all the way into the argv oarsub receives.

    Four Grid'5000 sites auto-select a queue that does not exist and reject the
    job outright, so a queue silently dropped on this path is not a cosmetic
    loss: it is the difference between a site working and being unusable.
    """
    _, run, snapshot = prepared
    bundle, paths = prepare_job(run, snapshot, SHARD, **REMOTE)
    observed: list[tuple[str, ...]] = []

    plan, result = submit_job(
        paths,
        bundle,
        policy=_verdict(),
        allowed_root=run,
        apply=True,
        queue="default",
        runner=_runner(  # type: ignore[arg-type]
            [CommandResult((), 0, "OAR_JOB_ID=6917617\n", "")], observed
        ),
    )

    assert result is not None
    assert observed, "the runner was never called"
    sent = observed[0]
    assert "-q" in sent
    assert sent[sent.index("-q") + 1] == "default"
    assert "-q" in plan.argv
    assert plan.argv[plan.argv.index("-q") + 1] == "default"


def test_an_applied_submission_without_a_queue_asks_for_none(
    prepared: tuple[Path, Path, SnapshotManifest],
) -> None:
    """Sophia refuses an explicit queue, so the bare form must stay reachable."""
    _, run, snapshot = prepared
    bundle, paths = prepare_job(run, snapshot, SHARD, **REMOTE)
    observed: list[tuple[str, ...]] = []

    _, result = submit_job(
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
    assert "-q" not in observed[0]


def test_a_planned_submission_shows_the_queue_it_would_request(
    prepared: tuple[Path, Path, SnapshotManifest],
) -> None:
    """The plan is what an operator reads before allowing the apply gate."""
    _, run, snapshot = prepared
    bundle, paths = prepare_job(run, snapshot, SHARD, **REMOTE)

    plan, result = submit_job(
        paths, bundle, policy=_verdict(), allowed_root=run, apply=False, queue="default"
    )

    assert result is None
    assert plan.argv[plan.argv.index("-q") + 1] == "default"
