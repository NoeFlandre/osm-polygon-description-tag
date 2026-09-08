"""Public payload and durable submission recovery boundaries, without a scheduler."""

import json
from dataclasses import replace

import pytest

from osm_polygon_description_tag.dataset.languages.checkpoint import (
    part_name_for_offset,
    shard_paths,
)
from osm_polygon_description_tag.workflow import grid_operator as operator
from osm_polygon_description_tag.workflow.grid_scheduler import CommandResult, SchedulerError
from tests.unit.workflow.test_grid_operator import REMOTE, SHARD, _verdict
from tests.unit.workflow.test_grid_operator import prepared as prepared


def _intent(**changes: object) -> dict[str, object]:
    return {
        "intent_schema_version": operator.INTENT_SCHEMA_VERSION,
        "bundle_id": "a" * 64,
        "shard": SHARD,
        "job_name": "lang-aaaaaaaaaaaaaaaa",
        "walltime_seconds": 3600,
        "cores": 1,
        "recorded_at": "2026-09-08T12:00:00+00:00",
        "job_id": None,
        "outcome": None,
        "detail": None,
        **changes,
    }


@pytest.mark.parametrize(
    ("changes", "message"),
    [
        ({"job_id": 0}, "intent field job_id must be a positive integer"),
        ({"job_id": True}, "intent field job_id must be an integer or null"),
        ({"attempt": 0}, "intent field attempt must be an integer >= 1"),
        ({"attempt": True}, "intent field attempt must be an integer >= 1"),
        ({"attempt": "2"}, "intent field attempt must be an integer >= 1"),
        ({"result_acknowledged": 1}, "intent field result_acknowledged must be a boolean"),
        ({"result_complete": "false"}, "intent field result_complete must be a boolean"),
        ({"reconciled_at": 3}, "intent field reconciled_at must be a string or null"),
        ({"collected_at": []}, "intent field collected_at must be a string or null"),
        ({"outcome": "success"}, "unsupported submission outcome: 'success'"),
        ({"terminal_state": "running"}, "intent terminal_state must be terminated"),
        ({"terminal_state": "terminated"}, "terminal intent must record a job identifier"),
        ({"result_acknowledged": True}, "collected results require a terminal scheduler state"),
        ({"result_complete": True}, "complete results require a collection acknowledgment"),
        ({"outcome": "rejected", "job_id": 1}, "rejected intent must not record a job identifier"),
    ],
)
def test_intent_rejects_contradictory_or_mistyped_recovery_fields(changes, message):
    with pytest.raises(operator.GridOperatorError) as error:
        operator.SubmissionIntent.from_payload(_intent(**changes))
    assert str(error.value) == message


def test_legacy_intent_defaults_do_not_authorize_retry():
    payload = _intent()
    intent = operator.SubmissionIntent.from_payload(payload)
    assert intent.to_payload() == {
        **payload,
        "attempt": 1,
        "terminal_state": None,
        "reconciled_at": None,
        "result_acknowledged": False,
        "result_complete": False,
        "collected_at": None,
    }
    assert intent.is_unresolved
    assert intent.is_active_or_ambiguous
    assert not intent.can_retry
    paused = replace(intent, job_id=7, terminal_state="terminated", result_acknowledged=True)
    assert paused.can_retry
    assert not paused.is_unresolved
    assert not paused.is_active_or_ambiguous
    assert not replace(paused, result_complete=True).can_retry


@pytest.mark.parametrize(
    ("field", "value", "message"),
    [
        ("snapshot_id", "A" * 64, "snapshot id must be a lowercase SHA-256 hex fingerprint"),
        (
            "model_config_fingerprint",
            "f" * 63,
            "model configuration fingerprint must be a lowercase SHA-256 hex fingerprint",
        ),
        (
            "code_fingerprint",
            "g" * 64,
            "code fingerprint must be a lowercase SHA-256 hex fingerprint",
        ),
        (
            "lock_fingerprint",
            "a" * 64 + "\n",
            "lock fingerprint must be a lowercase SHA-256 hex fingerprint",
        ),
        ("source_sha256", "", "source sha256 must be a lowercase SHA-256 hex fingerprint"),
        ("source_size_bytes", -1, "source size must be a non-negative integer"),
        ("input_row_count", -1, "input row count must be a non-negative integer"),
        ("shard", "region.csv", "shard must be a Parquet path"),
    ],
)
def test_bundle_validates_content_before_accepting_identity(field, value, message):
    bundle = operator.JobBundle("a" * 64, "b" * 64, "c" * 64, "d" * 64, SHARD, "e" * 64, 0, 0)
    with pytest.raises(operator.GridOperatorError) as error:
        operator.JobBundle.from_payload({**bundle.to_payload(), field: value})
    assert str(error.value) == message


@pytest.fixture
def job(request):
    _, run, snapshot = request.getfixturevalue("prepared")
    bundle, paths = operator.prepare_job(run, snapshot, SHARD, **REMOTE)
    return run, bundle, paths


def _write_intent(bundle, paths, **changes):
    payload = _intent(bundle_id=bundle.bundle_id, **changes)
    paths.intent.write_text(json.dumps(payload), encoding="utf-8")
    return paths.intent.read_bytes()


def _no_command(*args, **kwargs):
    pytest.fail("this boundary must not contact the scheduler")


@pytest.mark.parametrize("limit", [0, -1, True, 1.5, "2"])
def test_invalid_attempt_limit_fails_before_recording_or_submitting(job, limit):
    run, bundle, paths = job
    with pytest.raises(operator.GridOperatorError) as error:
        operator.submit_job(
            paths,
            bundle,
            policy=_verdict(),
            allowed_root=run,
            max_attempts=limit,
            apply=True,
            runner=_no_command,
        )
    assert str(error.value) == "maximum submission attempts must be a positive integer or null"
    assert not paths.intent.exists()
    with operator.submission_lock(run):
        pass


@pytest.mark.parametrize("resolved_id", [0, -1, True, "13", 1.5])
def test_invalid_name_resolution_preserves_intent_and_releases_lock(job, resolved_id):
    run, bundle, paths = job
    before = _write_intent(bundle, paths)
    names = []

    def resolve(name):
        names.append(name)
        return resolved_id

    with pytest.raises(operator.GridOperatorError) as error:
        operator.reconcile_job(paths, apply=True, resolve_job_name=resolve, runner=_no_command)
    assert str(error.value) == "job-name resolver must return a positive integer or null"
    assert names == ["lang-aaaaaaaaaaaaaaaa"]
    assert paths.intent.read_bytes() == before
    with operator.submission_lock(run):
        pass


def test_no_name_match_stays_ambiguous_and_blocks_a_retry(job):
    run, bundle, paths = job
    before = _write_intent(bundle, paths)
    result = operator.reconcile_job(
        paths, apply=True, resolve_job_name=lambda name: None, runner=_no_command
    )
    assert result.to_payload() == {
        "intent": operator.read_intent(paths.intent).to_payload(),
        "state": "unknown",
        "detail": "scheduler lookup by job name did not resolve a job; do not resubmit",
        "needs_operator_attention": True,
    }
    plan, submitted = operator.submit_job(
        paths, bundle, policy=_verdict(), allowed_root=run, apply=True, runner=_no_command
    )
    assert not plan.may_apply
    assert submitted is None
    assert paths.intent.read_bytes() == before


def test_scheduler_exception_keeps_durable_unresolved_intent(job):
    run, bundle, paths = job

    def interrupted(argv, timeout):
        assert operator.read_intent(paths.intent).is_unresolved
        raise RuntimeError("operator interrupted while waiting for scheduler")

    with pytest.raises(RuntimeError, match="operator interrupted"):
        operator.submit_job(
            paths, bundle, policy=_verdict(), allowed_root=run, apply=True, runner=interrupted
        )
    intent = operator.read_intent(paths.intent)
    assert intent.outcome is None
    assert intent.attempt == 1
    assert intent.is_unresolved
    with operator.submission_lock(run):
        pass
    plan, result = operator.submit_job(
        paths, bundle, policy=_verdict(), allowed_root=run, apply=True, runner=_no_command
    )
    assert not plan.may_apply
    assert result is None
    assert operator.read_intent(paths.intent) == intent


def test_resolved_identifier_survives_scheduler_query_failure(job):
    run, bundle, paths = job
    _write_intent(bundle, paths)

    def interrupted(argv, timeout):
        assert operator.read_intent(paths.intent).job_id == 73
        raise RuntimeError("query interrupted")

    with pytest.raises(RuntimeError, match="query interrupted"):
        operator.reconcile_job(
            paths, apply=True, resolve_job_name=lambda name: 73, runner=interrupted
        )
    intent = operator.read_intent(paths.intent)
    assert intent.job_id == 73
    assert intent.terminal_state is None
    with operator.submission_lock(run):
        pass


@pytest.mark.parametrize("kind", ["run-file", "run-link", "lock-directory", "lock-link"])
def test_submission_lock_rejects_nonregular_locations(tmp_path, kind):
    run = tmp_path / "run"
    if kind == "run-file":
        run.write_text("keep", encoding="utf-8")
    elif kind == "run-link":
        target = tmp_path / "target"
        target.mkdir()
        run.symlink_to(target, target_is_directory=True)
    else:
        run.mkdir()
        lock = run / operator.GRID_SUBMISSION_LOCK_FILENAME
        if kind == "lock-directory":
            lock.mkdir()
        else:
            lock.symlink_to(tmp_path / "missing-target")
    expected = (
        f"run directory is not a regular directory: {run}"
        if kind.startswith("run-")
        else f"submission lock must be a regular file: {lock}"
    )
    with pytest.raises(operator.GridOperatorError) as error, operator.submission_lock(run):
        pytest.fail("unsafe lock location was accepted")
    assert str(error.value) == expected


def test_reconcile_cannot_modify_an_intent_while_run_lock_is_held(job):
    run, bundle, paths = job
    before = _write_intent(bundle, paths)
    with operator.submission_lock(run), pytest.raises(operator.GridOperatorError) as error:
        operator.reconcile_job(paths, apply=True, runner=_no_command, resolve_job_name=_no_command)
    assert str(error.value) == f"submission lock is already held: {run}"
    assert paths.intent.read_bytes() == before


@pytest.mark.parametrize("field", ["bundle_id", "shard"])
def test_plan_refuses_a_foreign_intent_in_another_job_directory(job, field):
    run, bundle, paths = job
    other = run / "jobs" / "other"
    other.mkdir()
    (other / operator.BUNDLE_FILENAME).write_text(json.dumps(bundle.to_payload()), encoding="utf-8")
    payload = _intent(bundle_id=bundle.bundle_id)
    payload[field] = "f" * 64 if field == "bundle_id" else "other.parquet"
    intent_path = other / operator.INTENT_FILENAME
    intent_path.write_text(json.dumps(payload), encoding="utf-8")
    with pytest.raises(operator.GridOperatorError) as error:
        operator.plan_submission(paths, bundle, policy=_verdict(), allowed_root=run)
    assert str(error.value) == f"submission intent is not bound to its bundle: {intent_path}"
    assert not paths.intent.exists()


@pytest.mark.parametrize(
    "defect", ["missing-bundle", "bundle-id", "shard", "snapshot", "issues", "empty-report"]
)
def test_acknowledgment_rejection_preserves_intent_and_releases_lock(job, defect):
    run, bundle, paths = job
    before = _write_intent(bundle, paths, job_id=73, terminal_state="terminated")
    report = operator.collect_results(run, SHARD)
    if defect == "missing-bundle":
        paths.bundle.unlink()
    elif defect in {"bundle-id", "shard"}:
        changes = {"bundle_id": "f" * 64} if defect == "bundle-id" else {"shard": "other.parquet"}
        payload = json.loads(before)
        paths.intent.write_text(json.dumps({**payload, **changes}), encoding="utf-8")
        before = paths.intent.read_bytes()
    elif defect == "snapshot":
        report = replace(report, snapshot_id="f" * 64)
    elif defect == "issues":
        state = shard_paths(run, SHARD)
        state.parts.mkdir(parents=True, exist_ok=True)
        (state.parts / part_name_for_offset(0)).write_bytes(b"an uncheckpointed orphan")
        report = operator.collect_results(run, SHARD)
    elif defect == "empty-report":
        report = replace(report, shards=())
    expected = {
        "missing-bundle": "submission intent is not bound to the prepared bundle",
        "bundle-id": "submission intent is not bound to the prepared bundle",
        "shard": "submission intent is not bound to the prepared bundle",
        "snapshot": "collected results do not match the submitted bundle",
        "issues": "cannot acknowledge collection validation issues",
        "empty-report": "collection acknowledgment requires one shard report",
    }[defect]
    with pytest.raises(operator.GridOperatorError) as error:
        operator.acknowledge_collected_results(paths, report)
    assert str(error.value) == expected
    assert paths.intent.read_bytes() == before
    with operator.submission_lock(run):
        pass


def test_acknowledgment_obeys_the_run_wide_lock(job):
    run, bundle, paths = job
    before = _write_intent(bundle, paths, job_id=73, terminal_state="terminated")
    report = operator.collect_results(run, SHARD)
    with operator.submission_lock(run), pytest.raises(operator.GridOperatorError) as error:
        operator.acknowledge_collected_results(paths, report)
    assert str(error.value) == f"submission lock is already held: {run}"
    assert paths.intent.read_bytes() == before


def test_terminal_reconciliation_alone_does_not_authorize_retry(job):
    run, bundle, paths = job
    before = _write_intent(bundle, paths, job_id=73, terminal_state="terminated")
    result = operator.reconcile_job(paths, runner=_no_command)
    assert result.state is operator.JobState.TERMINATED
    assert result.detail == "terminal state was already reconciled"
    assert not result.needs_operator_attention
    plan = operator.plan_submission(paths, bundle, policy=_verdict(), allowed_root=run)
    assert not plan.may_apply
    assert plan.attempt == 1
    assert plan.blocked_reason == (
        "terminal job results must be collected and acknowledged before retrying"
    )
    assert paths.intent.read_bytes() == before


@pytest.mark.parametrize("has_intent", [False, True])
def test_live_reconciliation_without_an_identifier_never_guesses(job, has_intent):
    _, bundle, paths = job
    before = _write_intent(bundle, paths) if has_intent else None
    result = operator.reconcile_job(paths, apply=True, runner=_no_command)
    assert result.state is operator.JobState.UNKNOWN
    assert result.needs_operator_attention is has_intent
    assert result.detail == (
        "intent has no job identifier; query the scheduler by job name before resubmitting"
        if has_intent
        else "no submission intent has been recorded"
    )
    assert (paths.intent.read_bytes() if paths.intent.exists() else None) == before


@pytest.mark.parametrize(
    "defect", ["job-file", "intent-directory", "intent-link", "missing-bundle"]
)
def test_plan_fails_closed_on_unreadable_neighbor_state(job, defect):
    run, bundle, paths = job
    other = run / "jobs" / "other"
    intent = other / operator.INTENT_FILENAME
    if defect == "job-file":
        other.write_text("incomplete job directory", encoding="utf-8")
        expected = f"job directory must be a regular directory: {other}"
    else:
        other.mkdir()
        if defect == "intent-directory":
            intent.mkdir()
        elif defect == "intent-link":
            intent.symlink_to(run / "missing-intent")
        else:
            intent.write_text(json.dumps(_intent()), encoding="utf-8")
        expected = (
            f"submission intent has no bound bundle: {intent}"
            if defect == "missing-bundle"
            else f"submission intent must be a regular file: {intent}"
        )
    with pytest.raises(operator.GridOperatorError) as error:
        operator.plan_submission(paths, bundle, policy=_verdict(), allowed_root=run)
    assert str(error.value) == expected
    assert not paths.intent.exists()


@pytest.mark.parametrize(
    ("returncode", "timed_out", "message"),
    [
        (0, True, "account job lookup timed out; do not resubmit"),
        (23, True, "account job lookup timed out; do not resubmit"),
        (23, False, "account job lookup exited 23; do not resubmit"),
    ],
)
def test_name_lookup_refuses_partial_success_output(returncode, timed_out, message):
    calls = []

    def runner(argv, timeout):
        calls.append((tuple(argv), timeout))
        return CommandResult(
            tuple(argv),
            returncode,
            '{"73":{"name":"lang-target","state":"Running"}}',
            "scheduler connection interrupted",
            timed_out=timed_out,
        )

    with pytest.raises(operator.GridOperatorError) as error:
        operator.resolve_job_name("lang-target", runner=runner, timeout=2.75)
    assert str(error.value) == message
    assert calls == [(("oarstat", "-u", "-J"), 2.75)]


def test_name_lookup_preserves_command_failure_cause():
    failure = SchedulerError("oarstat executable is unavailable")
    calls = []

    def runner(argv, timeout):
        calls.append((tuple(argv), timeout))
        raise failure

    with pytest.raises(operator.GridOperatorError) as error:
        operator.resolve_job_name("lang-target", runner=runner, timeout=4.5)
    assert str(error.value) == (
        "cannot query account jobs for reconciliation: oarstat executable is unavailable"
    )
    assert error.value.__cause__ is failure
    assert calls == [(("oarstat", "-u", "-J"), 4.5)]


@pytest.mark.parametrize(
    ("stdout", "diagnostic"),
    [
        ("{incomplete", "account-wide oarstat output must be valid JSON"),
        ("[]", "account-wide oarstat output must be a JSON job map"),
        (
            '{"73":{"name":"lang-target","state":"Running"},'
            '"74":{"name":"lang-target","state":"Terminated"}}',
            "account-wide oarstat has multiple jobs named 'lang-target'; do not resubmit",
        ),
        (
            '{"73":{"name":"lang-target","state":"Running"},"74":{"name":"unrelated"}}',
            "account-wide oarstat job 74 has no state",
        ),
        (
            '{"73":{"name":"lang-target","state":"Running"},"73":{}}',
            "account-wide oarstat output has duplicate key '73'",
        ),
    ],
)
def test_name_lookup_rejects_ambiguous_or_incomplete_account_data(stdout, diagnostic):
    calls = []

    def runner(argv, timeout):
        calls.append(tuple(argv))
        return CommandResult(tuple(argv), 0, stdout, "")

    with pytest.raises(operator.GridOperatorError) as error:
        operator.resolve_job_name("lang-target", runner=runner)
    assert str(error.value) == f"cannot resolve account job by name: {diagnostic}"
    assert isinstance(error.value.__cause__, SchedulerError)
    assert str(error.value.__cause__) == diagnostic
    assert calls == [("oarstat", "-u", "-J")]


@pytest.mark.parametrize("exact_match", [False, True])
def test_name_lookup_matches_exact_name_among_retained_jobs(exact_match):
    records = {
        "71": {"name": "lang-target-old", "state": "Running"},
        "72": {"name": "Lang-target", "state": "Terminated"},
    }
    if exact_match:
        records["73"] = {"name": "lang-target", "state": "Terminated"}
    calls = []

    def runner(argv, timeout):
        calls.append((tuple(argv), timeout))
        return CommandResult(tuple(argv), 0, json.dumps(records), "")

    result = operator.resolve_job_name("lang-target", runner=runner, timeout=1.25)
    assert result == (73 if exact_match else None)
    assert calls == [(("oarstat", "-u", "-J"), 1.25)]


def test_account_lookup_timeout_preserves_recovery_state_and_reservation(job):
    run, bundle, paths = job
    before = _write_intent(bundle, paths)
    calls = []

    def runner(argv, timeout):
        calls.append(tuple(argv))
        return CommandResult(tuple(argv), 0, "{}", "", timed_out=True)

    def resolve(name):
        return operator.resolve_job_name(name, runner=runner)

    with pytest.raises(operator.GridOperatorError) as error:
        operator.reconcile_job(paths, apply=True, resolve_job_name=resolve, runner=_no_command)
    assert str(error.value) == "account job lookup timed out; do not resubmit"
    assert paths.intent.read_bytes() == before
    assert calls == [("oarstat", "-u", "-J")]
    with operator.submission_lock(run):
        pass
    plan, result = operator.submit_job(
        paths, bundle, policy=_verdict(), allowed_root=run, apply=True, runner=_no_command
    )
    assert not plan.may_apply
    assert result is None
    assert paths.intent.read_bytes() == before


@pytest.mark.parametrize("field", ["job_id", "outcome", "detail", "recorded_at", "bundle_id"])
def test_required_intent_fields_cannot_be_silently_defaulted(field):
    payload = _intent()
    del payload[field]
    with pytest.raises(operator.GridOperatorError) as error:
        operator.SubmissionIntent.from_payload(payload)
    assert str(error.value) == f"intent payload is missing {field}"


def test_collected_intent_round_trip_preserves_recovery_evidence():
    payload = _intent(
        job_id=73,
        outcome="submitted",
        detail="OAR_JOB_ID=73",
        attempt=2,
        terminal_state="terminated",
        reconciled_at="2026-09-08T12:10:00+00:00",
        result_acknowledged=True,
        result_complete=False,
        collected_at="2026-09-08T12:11:00+00:00",
    )
    intent = operator.SubmissionIntent.from_payload(json.loads(json.dumps(payload)))
    assert intent.to_payload() == payload
    assert intent.can_retry
    assert not intent.is_active_or_ambiguous
    assert not intent.is_unresolved
