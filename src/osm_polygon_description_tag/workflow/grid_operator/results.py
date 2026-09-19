"""Grid operator results responsibilities."""

from __future__ import annotations

from dataclasses import replace
from datetime import UTC, datetime
from pathlib import Path

from osm_polygon_description_tag.dataset.languages.atomic import atomic_write_json
from osm_polygon_description_tag.dataset.languages.checkpoint import ShardStatus
from osm_polygon_description_tag.dataset.languages.validation import RunReport, ShardReport
from osm_polygon_description_tag.workflow.grid_scheduler import JobState

from .bundle import _existing_bundle, read_intent
from .models import GridOperatorError, JobBundle, JobPaths, SubmissionIntent
from .script import job_paths
from .state import submission_lock


def adopt_retrieved_intent(
    paths: JobPaths, retrieved_run_dir: Path, bundle: JobBundle
) -> Path | None:
    """Bring the site's record of a submission into the owned run directory.

    A bounded job is submitted from the site's frontend against the site's copy
    of the run, so the durable intent is written there rather than here.
    Collection retrieves that copy; adopting its intent is what lets the owned
    run acknowledge the shard instead of seeing a bundle nothing ever claimed
    to submit.

    An intent already recorded here always wins. It is this run's own record,
    and it may already carry an acknowledgment that a retrieved copy predates.
    """
    if _owns_an_intent_already(paths):
        return None
    intent = _bound_retrieved_intent(job_paths(retrieved_run_dir, bundle).intent, bundle)
    with submission_lock(paths.run_dir):
        if paths.intent.exists():
            return None
        paths.root.mkdir(parents=True, exist_ok=True)
        atomic_write_json(paths.intent, intent.to_payload())
        return paths.intent


def _owns_an_intent_already(paths: JobPaths) -> bool:
    if paths.intent.is_symlink():
        raise GridOperatorError(f"submission intent must be a regular file: {paths.intent}")
    return paths.intent.exists()


def _bound_retrieved_intent(incoming: Path, bundle: JobBundle) -> SubmissionIntent:
    intent = _retrieved_intent(incoming)
    if intent.bundle_id != bundle.bundle_id or intent.shard != bundle.shard:
        raise GridOperatorError(
            f"retrieved submission intent is not bound to this bundle: {incoming}"
        )
    return intent


def _retrieved_intent(incoming: Path) -> SubmissionIntent:
    if incoming.is_symlink() or (incoming.exists() and not incoming.is_file()):
        raise GridOperatorError(f"submission intent must be a regular file: {incoming}")
    if not incoming.exists():
        raise GridOperatorError(f"retrieved run has no submission intent: {incoming}")
    return read_intent(incoming)


def acknowledge_collected_results(
    paths: JobPaths, report: RunReport, *, now: datetime | None = None
) -> SubmissionIntent:
    """Durably acknowledge one validated terminal result for a shard."""
    selected = _acknowledgment_shard(report)
    with submission_lock(paths.run_dir):
        intent, bundle = _bound_submission(paths)
        _validate_acknowledgment(report, selected, intent, bundle)
        updated = _acknowledge_intent(intent, report, now)
        atomic_write_json(paths.intent, updated.to_payload())
        return updated


def _acknowledgment_shard(report: RunReport) -> ShardReport:
    if not isinstance(report, RunReport) or len(report.shards) != 1:
        raise GridOperatorError("collection acknowledgment requires one shard report")
    return report.shards[0]


def _bound_submission(paths: JobPaths) -> tuple[SubmissionIntent, JobBundle]:
    intent = read_intent(paths.intent)
    bundle = _existing_bundle(paths)
    if bundle is None:
        raise GridOperatorError("submission intent is not bound to the prepared bundle")
    if intent.bundle_id != bundle.bundle_id or intent.shard != bundle.shard:
        raise GridOperatorError("submission intent is not bound to the prepared bundle")
    return intent, bundle


def _validate_acknowledgment(
    report: RunReport,
    selected: ShardReport,
    intent: SubmissionIntent,
    bundle: JobBundle,
) -> None:
    _validate_ack_identity(report, selected, bundle)
    _validate_ack_report_clean(report, selected)
    _validate_ack_terminal(selected, intent)
    if intent.result_complete and not report.is_complete:
        raise GridOperatorError("complete results cannot be downgraded to paused results")


def _validate_ack_identity(report: RunReport, selected: ShardReport, bundle: JobBundle) -> None:
    if report.snapshot_id != bundle.snapshot_id:
        raise GridOperatorError("collected results do not match the submitted bundle")
    if selected.shard != bundle.shard:
        raise GridOperatorError("collected results do not match the submitted bundle")


def _validate_ack_report_clean(report: RunReport, selected: ShardReport) -> None:
    if report.issues or selected.issues:
        raise GridOperatorError("cannot acknowledge collection validation issues")


def _validate_ack_terminal(selected: ShardReport, intent: SubmissionIntent) -> None:
    if selected.status not in {str(ShardStatus.PAUSED), str(ShardStatus.COMPLETE)}:
        raise GridOperatorError("collected results do not have a terminal checkpoint")
    if intent.terminal_state != str(JobState.TERMINATED):
        raise GridOperatorError("collected results require a terminal scheduler state")


def _acknowledge_intent(
    intent: SubmissionIntent, report: RunReport, now: datetime | None
) -> SubmissionIntent:
    return replace(
        intent,
        result_acknowledged=True,
        result_complete=report.is_complete,
        collected_at=(now or datetime.now(UTC)).isoformat(),
    )
