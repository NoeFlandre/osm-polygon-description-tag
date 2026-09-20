"""Exact blocking reasons and live reconciliation of one shard's submission.

A run directory holds 386 shards' jobs at once, so a blocking reason has to name
the shard and bundle it is about, and a reconciliation has to carry back the
intent it updated. Every value here is what an operator reads before deciding
whether a job may already be running.
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
from osm_polygon_description_tag.workflow.grid_scheduler import CommandResult
from tests.helpers.sentences import REMOTE_SAT_MODEL_PATH
from tests.unit.workflow.test_grid_operator import REMOTE, SHARD

_MOMENT = datetime(2026, 9, 15, 22, 0, tzinfo=UTC)


def _intent(bundle_id: str, **changes: object) -> SubmissionIntent:
    return replace(
        SubmissionIntent(
            bundle_id=bundle_id,
            shard=SHARD,
            job_name="lang-test",
            walltime_seconds=1800,
            cores=1,
            recorded_at="2026-09-15T21:00:00+00:00",
        ),
        **changes,
    )


def test_another_shards_unresolved_job_blocks_this_one_and_names_it(
    two_jobs: tuple[Path, tuple[object, object], tuple[object, object]],
) -> None:
    """The reason has to say which other shard and bundle to reconcile."""
    _, (first_bundle, first_paths), (second_bundle, second_paths) = two_jobs
    other = _intent(second_bundle.bundle_id, job_id=11, outcome="submitted")
    other = replace(other, shard=second_bundle.shard)
    second_paths.intent.write_text(json.dumps(other.to_payload()), encoding="utf-8")

    reason = operator._run_wide_intent_block(first_paths, first_bundle)

    assert reason == (
        f"another shard has an active or ambiguous submission "
        f"({second_bundle.shard}, bundle {second_bundle.bundle_id[:16]}); reconcile it first"
    )


def test_a_skipped_intent_does_not_stop_the_run_wide_scan(
    two_jobs: tuple[Path, tuple[object, object], tuple[object, object]],
) -> None:
    """This job's own intent is skipped; stopping there would miss every later one."""
    _, (first_bundle, first_paths), (second_bundle, second_paths) = two_jobs
    own = replace(
        _intent(first_bundle.bundle_id, job_id=9, outcome="submitted"),
        shard=first_bundle.shard,
    )
    first_paths.intent.write_text(json.dumps(own.to_payload()), encoding="utf-8")
    other = replace(
        _intent(second_bundle.bundle_id, job_id=11, outcome="submitted"),
        shard=second_bundle.shard,
    )
    second_paths.intent.write_text(json.dumps(other.to_payload()), encoding="utf-8")

    reason = operator._run_wide_intent_block(first_paths, first_bundle)

    assert reason == (
        f"another shard has an active or ambiguous submission "
        f"({second_bundle.shard}, bundle {second_bundle.bundle_id[:16]}); reconcile it first"
    )


def test_this_shards_own_intent_does_not_block_itself(
    job: tuple[Path, object, object],
) -> None:
    """Only *other* bundles block; a bundle blocking itself would strand every retry."""
    run, bundle, paths = job
    own = _intent(bundle.bundle_id, job_id=11, outcome="submitted")  # type: ignore[attr-defined]
    paths.intent.write_text(json.dumps(own.to_payload()), encoding="utf-8")  # type: ignore[attr-defined]

    assert operator._run_wide_intent_block(paths, bundle) is None  # type: ignore[arg-type]


def test_an_unprepared_shard_checkpoint_blocks_submission_with_its_exact_reason(
    job: tuple[Path, object, object],
) -> None:
    """Submitting without an initial checkpoint would run a job that resumes nothing."""
    run, bundle, paths = job
    state = operator.shard_paths(run, SHARD)
    state.checkpoint.unlink()

    assert operator._initial_checkpoint_block(paths, bundle) == (  # type: ignore[arg-type]
        "no initial shard checkpoint exists; prepare this job again before submitting"
    )


def test_a_live_reconciliation_returns_the_intent_it_updated_and_the_state_it_read(
    job: tuple[Path, object, object],
) -> None:
    """Losing the intent here would hide the job identifier the operator needs."""
    run, bundle, paths = job
    intent = _intent(bundle.bundle_id, job_id=13, outcome="submitted")  # type: ignore[attr-defined]
    paths.intent.write_text(json.dumps(intent.to_payload()), encoding="utf-8")  # type: ignore[attr-defined]

    def runner(argv: tuple[str, ...], timeout: float) -> CommandResult:
        return CommandResult(argv, returncode=0, stdout="13: Terminated", stderr="")

    reconciliation = operator.reconcile_job(
        paths,  # type: ignore[arg-type]
        apply=True,
        runner=runner,
        now=_MOMENT,
    )

    assert reconciliation.state is JobState.TERMINATED
    assert reconciliation.detail == "Terminated"
    assert reconciliation.intent is not None
    assert reconciliation.intent.job_id == 13
    assert reconciliation.intent.reconciled_at == "2026-09-15T22:00:00+00:00"


def test_an_acknowledgment_records_the_moment_the_caller_supplied(
    job: tuple[Path, object, object],
) -> None:
    """``acknowledge_collected_results`` must not substitute its own clock."""
    run, bundle, paths = job
    intent = _intent(
        bundle.bundle_id,  # type: ignore[attr-defined]
        job_id=13,
        outcome="submitted",
        terminal_state="terminated",
        reconciled_at="2026-09-15T21:30:00+00:00",
    )
    paths.intent.write_text(json.dumps(intent.to_payload()), encoding="utf-8")  # type: ignore[attr-defined]
    report = operator.collect_results(run, SHARD)

    updated = operator.acknowledge_collected_results(paths, report, now=_MOMENT)  # type: ignore[arg-type]

    assert updated.collected_at == "2026-09-15T22:00:00+00:00"
    recorded = json.loads(paths.intent.read_text(encoding="utf-8"))  # type: ignore[attr-defined]
    assert recorded["collected_at"] == "2026-09-15T22:00:00+00:00"


def test_a_prepared_job_carries_the_documented_default_budget_and_batch_size(
    request: pytest.FixtureRequest,
) -> None:
    """These defaults decide what one compute node actually computes."""
    _, run, snapshot = request.getfixturevalue("prepared")

    _, paths = operator.prepare_job(run, snapshot, SHARD, **REMOTE)

    script = paths.script.read_text(encoding="utf-8")
    assert "  --batch-size 512 \\\n" in script
    assert f"  --budget-seconds {operator.MAX_PROCESSING_SECONDS} \\\n" in script


def test_a_prepared_job_carries_the_requested_budget_and_batch_size(
    request: pytest.FixtureRequest,
) -> None:
    """A dropped keyword would silently fall back to the default budget."""
    _, run, snapshot = request.getfixturevalue("prepared")

    _, paths = operator.prepare_job(
        run,
        snapshot,
        SHARD,
        processing_seconds=900,
        batch_size=64,
        walltime_seconds=1200,
        **REMOTE,
    )

    script = paths.script.read_text(encoding="utf-8")
    assert "  --batch-size 64 \\\n" in script
    assert "  --budget-seconds 900 \\\n" in script
    config = json.loads((paths.root / operator.JOB_CONFIG_FILENAME).read_text(encoding="utf-8"))
    assert config["processing_seconds"] == 900
    assert config["batch_size"] == 64
    assert config["walltime_seconds"] == 1200


def test_a_failed_materialisation_reports_its_own_cause_not_its_cleanup(
    request: pytest.FixtureRequest, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Cleanup runs after the payload may already have been renamed away."""
    project, source, run, snapshot = request.getfixturevalue("portable_prepared")

    def fail_after_rename(_path: Path) -> None:
        raise OSError("the volume went away")

    monkeypatch.setattr(operator, "_fsync_directory", fail_after_rename)

    with pytest.raises(OSError, match="the volume went away"):
        operator.prepare_portable_job(
            run,
            project,
            source,
            snapshot,
            SHARD,
            remote_bundle_dir="/scratch/lang-bundle",
            sat_model_path=REMOTE_SAT_MODEL_PATH,
        )


def test_a_retrieved_checkpoint_that_rebinds_the_history_is_refused_exactly(
    job: tuple[Path, object, object],
) -> None:
    run, bundle, _ = job
    state = operator.shard_paths(run, SHARD)
    previous = operator.read_checkpoint(state.checkpoint)
    proposed = replace(previous, snapshot_id="f" * 64)

    with pytest.raises(GridOperatorError) as caught:
        operator._require_checkpoint_extension(previous, proposed)

    assert str(caught.value) == "retrieved checkpoint changes the committed history binding"


def test_a_committed_history_whose_rows_disagree_is_refused_exactly(
    job: tuple[Path, object, object],
) -> None:
    run, _, _ = job
    state = operator.shard_paths(run, SHARD)
    previous = replace(
        operator.read_checkpoint(state.checkpoint), annotation_count=3, completed_parts=()
    )

    with pytest.raises(GridOperatorError) as caught:
        operator._require_matching_committed_history(state, state, previous)

    assert str(caught.value) == "local annotation count differs from committed history"
