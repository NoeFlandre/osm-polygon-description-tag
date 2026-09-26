"""Crash recovery around the first checkpoint of one Grid'5000 language job.

Every scenario here is a local synthetic run: no scheduler, transport, or
production data is involved.
"""

import json
import os
from collections.abc import Sequence
from datetime import UTC, datetime
from pathlib import Path

import pytest

from osm_polygon_description_tag.dataset.languages.checkpoint import (
    ShardCheckpoint,
    ShardStatus,
    WorkerBusyError,
    exclusive_worker_lock,
    part_name_for_offset,
    read_checkpoint,
    receipt_name_for_part,
    shard_paths,
)
from osm_polygon_description_tag.dataset.languages.models import LanguageResult, LanguageStatus
from osm_polygon_description_tag.dataset.languages.snapshot import (
    SnapshotManifest,
    prepare_snapshot,
)
from osm_polygon_description_tag.dataset.languages.validation import validate_run
from osm_polygon_description_tag.dataset.languages.worker import ProcessingBudget, process_shard
from osm_polygon_description_tag.workflow import grid_operator
from osm_polygon_description_tag.workflow.grid_operator import (
    QUARANTINE_DIRNAME,
    GridOperatorError,
    acknowledge_collected_results,
    bundle_for_shard,
    collect_results,
    initialize_shard_checkpoint,
    job_paths,
    plan_submission,
    prepare_job,
    prepare_portable_job,
    quarantine_orphan_artifacts,
    read_intent,
    reconcile_job,
    submission_lock,
    submit_job,
)
from osm_polygon_description_tag.workflow.grid_policy import (
    PolicyDecision,
    PolicyEvidence,
    PolicyVerdict,
)
from osm_polygon_description_tag.workflow.grid_scheduler import CommandResult, JobState
from tests.helpers.parquet import write_description_shard
from tests.helpers.sentences import REMOTE_SAT_MODEL_PATH, fake_splitter

SHARD = "region.parquet"
REMOTE = {
    "remote_project_dir": "/home/user/project",
    "remote_source_dir": "/scratch/staging/source",
    "remote_run_dir": "/scratch/staging/run",
    "sat_model_path": "/home/user/models/sat-3l-sm/model.safetensors",
}
DETECTOR = lambda text: LanguageResult(  # noqa: E731
    "eng", 0.9, 0.1, 0.8, LanguageStatus.DETECTED, "detected"
)


def _verdict() -> PolicyVerdict:
    return PolicyVerdict(
        PolicyDecision.ALLOWED,
        ("test verdict",),
        PolicyEvidence(0, False, True, (), captured_at=datetime.now(UTC)),
    )


def _runner(results: list[CommandResult]) -> object:
    def _run(argv: Sequence[str], timeout: float) -> CommandResult:
        return results.pop(0)

    return _run


def _unbound_value(field: str) -> object:
    """Return a valid but foreign value for one checkpoint identity field."""
    return 99 if field == "input_row_count" else "c" * 64


def _write_shard(path: Path, rows: int) -> None:
    write_description_shard(
        path,
        rows,
        batch_size=2,
        tags=lambda index: {"description": f"A synthetic description {index}"},
    )


def test_preparing_a_job_initializes_a_paused_zero_cursor_checkpoint(
    prepared: tuple[Path, Path, SnapshotManifest],
) -> None:
    _, run, snapshot = prepared

    bundle, _ = prepare_job(run, snapshot, SHARD, batch_size=2, **REMOTE)

    checkpoint = read_checkpoint(shard_paths(run, SHARD).checkpoint)
    assert checkpoint.status == "paused"
    assert checkpoint.input_cursor == 0
    assert checkpoint.annotation_count == 0
    assert checkpoint.completed_parts == ()
    assert checkpoint.batch_size == 2
    assert checkpoint.input_row_count == bundle.input_row_count
    assert checkpoint.snapshot_id == snapshot.snapshot_id


def test_preparing_a_zero_row_shard_initializes_a_complete_checkpoint(
    empty_prepared: tuple[Path, Path, SnapshotManifest],
) -> None:
    _, run, snapshot = empty_prepared

    prepare_job(run, snapshot, SHARD, batch_size=2, **REMOTE)

    checkpoint = read_checkpoint(shard_paths(run, SHARD).checkpoint)
    assert checkpoint.is_complete
    assert checkpoint.input_row_count == 0
    assert collect_results(run, SHARD).is_complete


def test_repeated_preparation_preserves_committed_progress(
    prepared: tuple[Path, Path, SnapshotManifest],
) -> None:
    source, run, snapshot = prepared
    prepare_job(run, snapshot, SHARD, batch_size=2, **REMOTE)
    process_shard(
        run,
        source,
        SHARD,
        detector=DETECTOR,
        splitter=fake_splitter(),
        snapshot=snapshot,
        batch_size=2,
    )
    committed = shard_paths(run, SHARD).checkpoint.read_bytes()

    prepare_job(run, snapshot, SHARD, batch_size=2, **REMOTE)

    assert shard_paths(run, SHARD).checkpoint.read_bytes() == committed


def test_initializing_the_checkpoint_twice_writes_it_once(
    prepared: tuple[Path, Path, SnapshotManifest],
) -> None:
    _, run, snapshot = prepared
    bundle = bundle_for_shard(snapshot, SHARD)

    first = initialize_shard_checkpoint(run, bundle, batch_size=2)
    written = shard_paths(run, SHARD).checkpoint.read_bytes()
    second = initialize_shard_checkpoint(run, bundle, batch_size=512)

    assert first == second
    assert shard_paths(run, SHARD).checkpoint.read_bytes() == written


@pytest.mark.parametrize("fault", ["symlink", "corrupt", "directory", "conflicting", "orphan"])
def test_initialization_fails_closed_and_prevents_submission(
    prepared: tuple[Path, Path, SnapshotManifest], fault: str, tmp_path: Path
) -> None:
    _, run, snapshot = prepared
    state = shard_paths(run, SHARD)
    state.root.mkdir(parents=True)
    if fault == "symlink":
        state.checkpoint.symlink_to(tmp_path / "missing.json")
    elif fault == "corrupt":
        state.checkpoint.write_bytes(b"not JSON")
    elif fault == "directory":
        state.checkpoint.mkdir()
    elif fault == "conflicting":
        payload = ShardCheckpoint(
            snapshot_id="c" * 64,
            model_config_fingerprint=snapshot.model_config_fingerprint,
            shard=SHARD,
            batch_size=2,
            input_row_count=4,
            input_cursor=0,
            annotation_count=0,
            completed_parts=(),
            status=ShardStatus.PAUSED,
        ).to_payload()
        state.checkpoint.write_text(json.dumps(payload), encoding="utf-8")
    else:
        state.parts.mkdir(parents=True)
        (state.parts / part_name_for_offset(0)).write_bytes(b"orphan")

    with pytest.raises(GridOperatorError):
        prepare_job(run, snapshot, SHARD, batch_size=2, **REMOTE)
    assert not job_paths(run, bundle_for_shard(snapshot, SHARD)).intent.exists()


def test_submission_is_blocked_when_the_initial_checkpoint_is_missing(
    prepared: tuple[Path, Path, SnapshotManifest],
) -> None:
    _, run, snapshot = prepared
    bundle, paths = prepare_job(run, snapshot, SHARD, batch_size=2, **REMOTE)
    shard_paths(run, SHARD).checkpoint.unlink()

    plan = plan_submission(paths, bundle, policy=_verdict(), allowed_root=run)

    assert not plan.may_apply
    assert plan.blocked_reason is not None
    assert "checkpoint" in plan.blocked_reason


def test_initialization_holds_the_submission_lock_before_the_worker_lock(
    prepared: tuple[Path, Path, SnapshotManifest], monkeypatch: pytest.MonkeyPatch
) -> None:
    _, run, snapshot = prepared
    order: list[str] = []
    original_submission = grid_operator.submission_lock
    original_worker = grid_operator.exclusive_worker_lock

    def traced_submission(target: Path) -> object:
        order.append("submission")
        return original_submission(target)

    def traced_worker(target: Path) -> object:
        order.append("worker")
        return original_worker(target)

    monkeypatch.setattr(grid_operator, "submission_lock", traced_submission)
    monkeypatch.setattr(grid_operator, "exclusive_worker_lock", traced_worker)

    prepare_job(run, snapshot, SHARD, batch_size=2, **REMOTE)

    assert order == ["submission", "worker"]


def test_initialization_refuses_a_concurrently_locked_worker(
    prepared: tuple[Path, Path, SnapshotManifest],
) -> None:
    _, run, snapshot = prepared

    with exclusive_worker_lock(run), pytest.raises(WorkerBusyError):
        prepare_job(run, snapshot, SHARD, batch_size=2, **REMOTE)

    assert not shard_paths(run, SHARD).checkpoint.exists()


def test_initialization_refuses_a_concurrently_held_submission_lock(
    prepared: tuple[Path, Path, SnapshotManifest],
) -> None:
    _, run, snapshot = prepared

    with submission_lock(run), pytest.raises(GridOperatorError, match="already held"):
        prepare_job(run, snapshot, SHARD, batch_size=2, **REMOTE)


@pytest.mark.parametrize("stage", ["part", "receipt"])
def test_a_crash_before_the_checkpoint_is_quarantined_and_restageable(
    portable: tuple[Path, Path, Path, SnapshotManifest], stage: str
) -> None:
    project, source, run, snapshot = portable
    prepare_portable_job(
        run,
        project,
        source,
        snapshot,
        SHARD,
        batch_size=2,
        remote_bundle_dir="/scratch/lang-bundle",
        sat_model_path=REMOTE_SAT_MODEL_PATH,
    )
    state = shard_paths(run, SHARD)
    orphan_part = part_name_for_offset(0)
    state.parts.mkdir(parents=True, exist_ok=True)
    (state.parts / orphan_part).write_bytes(b"orphan part")
    orphan_names = [f"parts/{orphan_part}"]
    if stage == "receipt":
        state.receipts.mkdir(parents=True, exist_ok=True)
        (state.receipts / receipt_name_for_part(orphan_part)).write_bytes(b"{}")
        orphan_names.append(f"receipts/{receipt_name_for_part(orphan_part)}")

    quarantined = quarantine_orphan_artifacts(run, SHARD)

    assert list(quarantined) == orphan_names
    for name in orphan_names:
        assert (state.root / QUARANTINE_DIRNAME / name).is_file()
        assert not (state.root / name).exists()
    assert not collect_results(run, SHARD).shards[0].issues

    restaged = prepare_portable_job(
        run,
        project,
        source,
        snapshot,
        SHARD,
        batch_size=2,
        remote_bundle_dir="/scratch/lang-bundle",
        sat_model_path=REMOTE_SAT_MODEL_PATH,
    )
    staged_root = shard_paths(restaged.run_root, SHARD).root
    assert (staged_root / "checkpoint.json").is_file()
    assert not (staged_root / QUARANTINE_DIRNAME).exists()


def test_quarantine_preserves_committed_history_and_is_idempotent(
    prepared: tuple[Path, Path, SnapshotManifest],
) -> None:
    source, run, snapshot = prepared
    prepare_job(run, snapshot, SHARD, batch_size=2, **REMOTE)
    process_shard(
        run,
        source,
        SHARD,
        detector=DETECTOR,
        splitter=fake_splitter(),
        snapshot=snapshot,
        batch_size=2,
    )
    state = shard_paths(run, SHARD)
    committed = {path.name: path.read_bytes() for path in state.parts.iterdir()}

    assert quarantine_orphan_artifacts(run, SHARD) == ()
    assert quarantine_orphan_artifacts(run, SHARD) == ()

    assert {path.name: path.read_bytes() for path in state.parts.iterdir()} == committed
    assert not (state.root / QUARANTINE_DIRNAME).exists()
    assert collect_results(run, SHARD).is_complete


@pytest.mark.parametrize("fault", ["unknown", "symlink", "fifo", "no_checkpoint"])
def test_quarantine_rejects_unrecognized_artifacts(
    prepared: tuple[Path, Path, SnapshotManifest], fault: str, tmp_path: Path
) -> None:
    _, run, snapshot = prepared
    if fault != "no_checkpoint":
        prepare_job(run, snapshot, SHARD, batch_size=2, **REMOTE)
    state = shard_paths(run, SHARD)
    state.parts.mkdir(parents=True, exist_ok=True)
    if fault == "unknown":
        (state.parts / "notes.txt").write_bytes(b"unknown")
    elif fault == "symlink":
        (state.parts / part_name_for_offset(0)).symlink_to(tmp_path / "missing")
    elif fault == "fifo":
        os.mkfifo(state.parts / part_name_for_offset(0))
    else:
        (state.parts / part_name_for_offset(0)).write_bytes(b"orphan")

    with pytest.raises(GridOperatorError):
        quarantine_orphan_artifacts(run, SHARD)


def test_quarantine_refuses_to_overwrite_an_already_quarantined_artifact(
    prepared: tuple[Path, Path, SnapshotManifest],
) -> None:
    _, run, snapshot = prepared
    prepare_job(run, snapshot, SHARD, batch_size=2, **REMOTE)
    state = shard_paths(run, SHARD)
    orphan = part_name_for_offset(0)
    state.parts.mkdir(parents=True, exist_ok=True)
    (state.parts / orphan).write_bytes(b"first orphan")
    assert quarantine_orphan_artifacts(run, SHARD) == (f"parts/{orphan}",)
    (state.parts / orphan).write_bytes(b"second orphan")

    with pytest.raises(GridOperatorError, match="quarantine already holds"):
        quarantine_orphan_artifacts(run, SHARD)

    quarantined = state.root / QUARANTINE_DIRNAME / "parts" / orphan
    assert quarantined.read_bytes() == b"first orphan"
    assert (state.parts / orphan).read_bytes() == b"second orphan"


def test_quarantine_ignores_a_pristine_shard_without_a_checkpoint(
    prepared: tuple[Path, Path, SnapshotManifest],
) -> None:
    _, run, _ = prepared

    assert quarantine_orphan_artifacts(run, SHARD) == ()

    assert not shard_paths(run, SHARD).checkpoint.exists()


@pytest.mark.parametrize(
    "field", ["snapshot_id", "model_config_fingerprint", "shard", "input_row_count"]
)
def test_submission_is_blocked_by_an_unbound_or_unusable_checkpoint(
    prepared: tuple[Path, Path, SnapshotManifest], field: str
) -> None:
    _, run, snapshot = prepared
    bundle, paths = prepare_job(run, snapshot, SHARD, batch_size=2, **REMOTE)
    checkpoint = shard_paths(run, SHARD).checkpoint
    payload = json.loads(checkpoint.read_bytes())
    payload[field] = "other.parquet" if field == "shard" else _unbound_value(field)
    checkpoint.write_text(json.dumps(payload), encoding="utf-8")

    plan = plan_submission(paths, bundle, policy=_verdict(), allowed_root=run)

    assert not plan.may_apply
    assert plan.blocked_reason == "existing shard checkpoint is not bound to this job bundle"


def test_submission_is_blocked_by_a_corrupt_checkpoint(
    prepared: tuple[Path, Path, SnapshotManifest],
) -> None:
    _, run, snapshot = prepared
    bundle, paths = prepare_job(run, snapshot, SHARD, batch_size=2, **REMOTE)
    shard_paths(run, SHARD).checkpoint.write_bytes(b"not JSON")

    plan = plan_submission(paths, bundle, policy=_verdict(), allowed_root=run)

    assert not plan.may_apply
    assert plan.blocked_reason is not None
    assert plan.blocked_reason.startswith("shard checkpoint is unusable")


def test_validate_run_still_reports_uncheckpointed_orphans(
    prepared: tuple[Path, Path, SnapshotManifest],
) -> None:
    _, run, snapshot = prepared
    prepare_job(run, snapshot, SHARD, batch_size=2, **REMOTE)
    state = shard_paths(run, SHARD)
    state.parts.mkdir(parents=True, exist_ok=True)
    (state.parts / part_name_for_offset(0)).write_bytes(b"orphan")

    report = validate_run(run, shards=[SHARD])

    assert any("not recorded by the checkpoint" in issue for issue in report.shards[0].issues)


def test_a_setup_failure_before_the_first_batch_can_be_collected_and_retried(
    prepared: tuple[Path, Path, SnapshotManifest],
) -> None:
    _, run, snapshot = prepared
    bundle, paths = prepare_job(run, snapshot, SHARD, batch_size=2, **REMOTE)
    submit_job(
        paths,
        bundle,
        policy=_verdict(),
        allowed_root=run,
        apply=True,
        runner=_runner([CommandResult((), 0, "OAR_JOB_ID=41\n", "")]),  # type: ignore[arg-type]
    )
    reconciled = reconcile_job(
        paths,
        apply=True,
        runner=_runner([CommandResult((), 0, "41: Terminated\n", "")]),  # type: ignore[arg-type]
    )
    assert reconciled.state is JobState.TERMINATED

    report = collect_results(run, SHARD)
    assert not report.issues
    assert not report.is_complete

    acknowledgment = acknowledge_collected_results(paths, report)
    assert acknowledgment.result_acknowledged
    assert not acknowledgment.result_complete

    retry = plan_submission(paths, bundle, policy=_verdict(), allowed_root=run, max_attempts=2)
    assert retry.may_apply
    assert retry.attempt == 2
    exhausted = plan_submission(paths, bundle, policy=_verdict(), allowed_root=run, max_attempts=1)
    assert not exhausted.may_apply


def test_an_ambiguous_submission_is_never_resubmitted_after_a_setup_failure(
    prepared: tuple[Path, Path, SnapshotManifest],
) -> None:
    _, run, snapshot = prepared
    bundle, paths = prepare_job(run, snapshot, SHARD, batch_size=2, **REMOTE)
    submit_job(
        paths,
        bundle,
        policy=_verdict(),
        allowed_root=run,
        apply=True,
        runner=_runner([CommandResult((), 0, "no identifier here\n", "")]),  # type: ignore[arg-type]
    )
    assert read_intent(paths.intent).is_unresolved

    plan = plan_submission(paths, bundle, policy=_verdict(), allowed_root=run)

    assert not plan.may_apply
    assert plan.blocked_reason is not None
    assert "reconcile" in plan.blocked_reason


def test_resumed_output_is_byte_identical_to_an_uninterrupted_run(tmp_path: Path) -> None:
    source = tmp_path / "source"
    source.mkdir(parents=True)
    _write_shard(source / SHARD, 6)
    straight = prepare_snapshot(
        source, tmp_path / "straight", code_fingerprint="a" * 64, lock_fingerprint="b" * 64
    )
    resumed = prepare_snapshot(
        source, tmp_path / "resumed", code_fingerprint="a" * 64, lock_fingerprint="b" * 64
    )
    process_shard(
        tmp_path / "straight",
        source,
        SHARD,
        detector=DETECTOR,
        splitter=fake_splitter(),
        snapshot=straight,
        batch_size=2,
    )
    prepare_job(tmp_path / "resumed", resumed, SHARD, batch_size=2, **REMOTE)
    for _ in range(3):
        process_shard(
            tmp_path / "resumed",
            source,
            SHARD,
            detector=DETECTOR,
            splitter=fake_splitter(),
            snapshot=resumed,
            batch_size=2,
            budget=ProcessingBudget(1, clock=iter([0.0, 0.0, 0.0, 3.0]).__next__),
        )

    straight_state = shard_paths(tmp_path / "straight", SHARD)
    resumed_state = shard_paths(tmp_path / "resumed", SHARD)
    assert collect_results(tmp_path / "resumed", SHARD).is_complete
    assert straight_state.checkpoint.read_bytes() == resumed_state.checkpoint.read_bytes()
    assert {path.name: path.read_bytes() for path in straight_state.parts.iterdir()} == {
        path.name: path.read_bytes() for path in resumed_state.parts.iterdir()
    }
    assert {path.name: path.read_bytes() for path in straight_state.receipts.iterdir()} == {
        path.name: path.read_bytes() for path in resumed_state.receipts.iterdir()
    }
