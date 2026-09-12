"""Exact durable state and refusals around one shard's submission.

Everything here decides whether an operator resubmits a job that may already be
running on Grid'5000. The intent file is the only durable record of that, so the
values written into it, the reasons that block a retry, and the locks taken
around those writes are each asserted by value.
"""

from __future__ import annotations

import json
from dataclasses import replace
from datetime import UTC, datetime
from pathlib import Path

import pytest

from osm_polygon_description_tag.workflow import grid_operator as operator
from osm_polygon_description_tag.workflow.grid_operator import (
    GridOperatorError,
    JobState,
    SubmissionIntent,
)
from osm_polygon_description_tag.workflow.grid_policy import (
    PolicyDecision,
    PolicyEvidence,
    PolicyVerdict,
)
from osm_polygon_description_tag.workflow.grid_scheduler import SubmissionOutcome, SubmissionResult
from tests.unit.workflow.test_grid_operator import REMOTE, SHARD, _verdict
from tests.unit.workflow.test_grid_operator import prepared as prepared

_MOMENT = datetime(2026, 9, 15, 22, 0, tzinfo=UTC)


@pytest.fixture
def job(request: pytest.FixtureRequest) -> tuple[Path, object, object]:
    _, run, snapshot = request.getfixturevalue("prepared")
    bundle, paths = operator.prepare_job(run, snapshot, SHARD, **REMOTE)
    return run, bundle, paths


def _intent(bundle: object, **changes: object) -> SubmissionIntent:
    return replace(
        SubmissionIntent(
            bundle_id=bundle.bundle_id,  # type: ignore[attr-defined]
            shard=SHARD,
            job_name="lang-test",
            walltime_seconds=1800,
            cores=1,
            recorded_at="2026-09-15T21:00:00+00:00",
        ),
        **changes,
    )


def test_an_acknowledgment_records_the_moment_it_was_given(
    job: tuple[Path, object, object],
) -> None:
    """Without ``collected_at`` an operator cannot tell an old receipt from a new one."""
    _, bundle, _ = job
    intent = _intent(bundle, job_id=7, terminal_state="terminated")
    report = type("Report", (), {"is_complete": True})()

    updated = operator._acknowledge_intent(intent, report, _MOMENT)  # type: ignore[arg-type]

    assert updated.collected_at == "2026-09-15T22:00:00+00:00"
    assert updated.result_acknowledged is True
    assert updated.result_complete is True


def test_an_acknowledgment_without_a_clock_records_an_offset_aware_moment(
    job: tuple[Path, object, object],
) -> None:
    """A naive local timestamp cannot be compared with the site's own records."""
    _, bundle, _ = job
    intent = _intent(bundle, job_id=7, terminal_state="terminated")
    report = type("Report", (), {"is_complete": False})()

    updated = operator._acknowledge_intent(intent, report, None)  # type: ignore[arg-type]

    assert updated.collected_at is not None
    assert datetime.fromisoformat(updated.collected_at).tzinfo is not None


def test_a_terminal_reconciliation_records_the_moment_it_happened(
    job: tuple[Path, object, object],
) -> None:
    _, bundle, paths = job
    intent = _intent(bundle, job_id=7)

    updated = operator._record_terminal_reconciliation(
        paths,  # type: ignore[arg-type]
        intent,
        JobState.TERMINATED,
        _MOMENT,
    )

    assert updated.terminal_state == "terminated"
    assert updated.reconciled_at == "2026-09-15T22:00:00+00:00"
    recorded = json.loads(paths.intent.read_text(encoding="utf-8"))  # type: ignore[attr-defined]
    assert recorded["reconciled_at"] == "2026-09-15T22:00:00+00:00"


def test_a_terminal_reconciliation_without_a_clock_is_offset_aware(
    job: tuple[Path, object, object],
) -> None:
    _, bundle, paths = job
    intent = _intent(bundle, job_id=7)

    updated = operator._record_terminal_reconciliation(
        paths,  # type: ignore[arg-type]
        intent,
        JobState.TERMINATED,
        None,
    )

    assert updated.reconciled_at is not None
    assert datetime.fromisoformat(updated.reconciled_at).tzinfo is not None


def test_a_recorded_outcome_keeps_the_schedulers_own_detail(
    job: tuple[Path, object, object],
) -> None:
    """The detail is what tells an operator whether a job may already exist."""
    _, bundle, paths = job
    intent = _intent(bundle)
    result = SubmissionResult(
        SubmissionOutcome.AMBIGUOUS,
        None,
        ("oarsub",),
        "oarsub timed out; a job may or may not be queued, so do not resubmit",
    )

    operator._record_outcome(paths, intent, result)  # type: ignore[arg-type]

    recorded = json.loads(paths.intent.read_text(encoding="utf-8"))  # type: ignore[attr-defined]
    assert recorded["detail"] == (
        "oarsub timed out; a job may or may not be queued, so do not resubmit"
    )
    assert recorded["outcome"] == "ambiguous"


def test_a_dry_reconciliation_of_an_unwritten_intent_says_so_exactly(
    job: tuple[Path, object, object],
) -> None:
    _, _, paths = job

    reconciliation = operator.reconcile_job(paths)  # type: ignore[arg-type]

    assert reconciliation.intent is None
    assert reconciliation.state is JobState.UNKNOWN
    assert reconciliation.detail == "no submission intent has been recorded"


def test_a_dry_reconciliation_of_a_live_intent_keeps_it_and_says_what_is_needed(
    job: tuple[Path, object, object],
) -> None:
    """The intent has to come back: it is what the operator reconciles against."""
    _, bundle, paths = job
    intent = _intent(bundle, job_id=7, outcome="submitted")
    paths.intent.write_text(json.dumps(intent.to_payload()), encoding="utf-8")  # type: ignore[attr-defined]

    reconciliation = operator.reconcile_job(paths)  # type: ignore[arg-type]

    assert reconciliation.intent == intent
    assert reconciliation.state is JobState.UNKNOWN
    assert reconciliation.detail == "pass the apply gate to query the scheduler"


def test_a_dry_reconciliation_of_an_already_terminal_intent_keeps_it(
    job: tuple[Path, object, object],
) -> None:
    _, bundle, paths = job
    intent = _intent(bundle, job_id=7, terminal_state="terminated")
    paths.intent.write_text(json.dumps(intent.to_payload()), encoding="utf-8")  # type: ignore[attr-defined]

    reconciliation = operator.reconcile_job(paths)  # type: ignore[arg-type]

    assert reconciliation.intent == intent
    assert reconciliation.state is JobState.TERMINATED
    assert reconciliation.detail == "terminal state was already reconciled"


def test_an_unresolved_intent_without_a_job_id_says_what_to_reconcile(
    job: tuple[Path, object, object],
) -> None:
    _, bundle, _ = job

    assert operator._active_intent_reason(_intent(bundle)) == (
        "a previous submission left an unresolved intent; "
        "reconcile whether a job exists before submitting again"
    )


def test_an_unresolved_intent_with_a_job_id_names_that_job(
    job: tuple[Path, object, object],
) -> None:
    _, bundle, _ = job

    assert operator._active_intent_reason(_intent(bundle, job_id=7)) == (
        "job 7 was already submitted for this bundle; reconcile it first"
    )


def test_an_already_complete_shard_is_not_retried(job: tuple[Path, object, object]) -> None:
    _, bundle, _ = job
    intent = _intent(
        bundle,
        job_id=7,
        terminal_state="terminated",
        result_acknowledged=True,
        result_complete=True,
    )

    assert operator._intent_retry_block(intent, None) == (
        "complete results have already been acknowledged for this shard"
    )


@pytest.mark.parametrize(
    ("captured_at", "reason"),
    [
        (None, "policy evidence freshness is unknown"),
        (datetime(2020, 1, 1, tzinfo=UTC), "policy evidence is stale or from the future"),
    ],
)
def test_unusable_policy_freshness_is_named_exactly(
    captured_at: datetime | None, reason: str
) -> None:
    verdict = PolicyVerdict(
        PolicyDecision.ALLOWED,
        ("test verdict",),
        PolicyEvidence(0, False, True, (), captured_at=captured_at),
    )

    assert operator._policy_freshness_block(verdict, _MOMENT) == reason


def test_a_plan_names_the_bundle_and_shard_it_would_submit(
    job: tuple[Path, object, object],
) -> None:
    """A plan for another shard would authorise the wrong job."""
    run, bundle, paths = job

    plan = operator.plan_submission(
        paths,  # type: ignore[arg-type]
        bundle,  # type: ignore[arg-type]
        policy=_verdict(),
        allowed_root=run,
    )

    assert plan.bundle_id == bundle.bundle_id  # type: ignore[attr-defined]
    assert plan.shard == SHARD


def test_a_plan_does_not_demand_fresh_evidence_unless_it_is_asked_to(
    job: tuple[Path, object, object],
) -> None:
    """Freshness is required only behind the apply gate; a dry plan must not block."""
    run, bundle, paths = job
    verdict = PolicyVerdict(
        PolicyDecision.ALLOWED,
        ("test verdict",),
        PolicyEvidence(0, False, True, (), captured_at=None),
    )

    plan = operator.plan_submission(
        paths,  # type: ignore[arg-type]
        bundle,  # type: ignore[arg-type]
        policy=verdict,
        allowed_root=run,
    )

    assert plan.blocked_reason is None


def test_initializing_a_checkpoint_takes_the_run_wide_submission_lock(
    job: tuple[Path, object, object],
) -> None:
    """Assuming the lock is held would let two operators initialise the same shard."""
    run, bundle, _ = job

    with operator.submission_lock(run), pytest.raises(GridOperatorError) as caught:
        operator.initialize_shard_checkpoint(run, bundle, batch_size=2)  # type: ignore[arg-type]

    assert str(caught.value) == f"submission lock is already held: {run}"


def test_quarantining_orphans_takes_the_run_wide_submission_lock(
    job: tuple[Path, object, object],
) -> None:
    run, _, _ = job

    with operator.submission_lock(run), pytest.raises(GridOperatorError) as caught:
        operator.quarantine_orphan_artifacts(run, SHARD)

    assert str(caught.value) == f"submission lock is already held: {run}"
