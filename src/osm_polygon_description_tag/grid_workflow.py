"""Grid prepare, stage, submit, status, and collection workflows."""

from __future__ import annotations

from collections.abc import Sequence
from pathlib import Path

from osm_polygon_description_tag.dataset.languages.snapshot import SnapshotManifest, read_snapshot
from osm_polygon_description_tag.dataset.languages.validation import RunReport
from osm_polygon_description_tag.grid_transport import (
    _capture_policy,
    _execute_transport,
    _portable_remote_paths,
    _remote_child,
    _seed_retrieval_snapshot,
    _transport_payload,
    _utc_now,
    grid_command_runner,
)
from osm_polygon_description_tag.runtime.presentation import print_json
from osm_polygon_description_tag.workflow.grid_operator import (
    GridOperatorError,
    JobBundle,
    JobPaths,
    SubmissionIntent,
    acknowledge_collected_results,
    adopt_retrieved_intent,
    build_bundle_transfer_argv,
    build_result_retrieval_argv,
    bundle_for_shard,
    collect_results,
    import_retrieved_results,
    job_paths,
    jobs_root,
    prepare_job,
    prepare_portable_job,
    quarantine_orphan_artifacts,
    reconcile_job,
    resolve_job_name,
    submit_job,
    verify_prepared_bundle,
)
from osm_polygon_description_tag.workflow.grid_policy import evaluate_policy
from osm_polygon_description_tag.workflow.grid_scheduler import CommandRunner


def handle_grid_prepare(
    run_dir: Path,
    shard: str,
    remote_project_dir: str,
    remote_source_dir: str,
    remote_run_dir: str,
    processing_seconds: int,
    batch_size: int,
    sat_model_path: str,
    glotlid_model_path: str | None = None,
) -> None:
    """Write the immutable bundle and job script for one shard."""
    snapshot = read_snapshot(run_dir)
    bundle, paths = prepare_job(
        run_dir,
        snapshot,
        shard,
        remote_project_dir=remote_project_dir,
        remote_source_dir=remote_source_dir,
        remote_run_dir=remote_run_dir,
        processing_seconds=processing_seconds,
        batch_size=batch_size,
        sat_model_path=sat_model_path,
        glotlid_model_path=glotlid_model_path,
    )
    print_json(
        {
            "bundle_id": bundle.bundle_id,
            "shard": bundle.shard,
            "input_row_count": bundle.input_row_count,
            "job_dir": str(paths.root),
            "script": str(paths.script),
        }
    )


def handle_grid_submit(
    run_dir: Path,
    shard: str,
    remote_project_dir: str,
    remote_source_dir: str,
    remote_run_dir: str,
    site: str,
    walltime_seconds: int,
    processing_seconds: int,
    batch_size: int,
    allow_daytime: bool,
    apply: bool,
    *,
    runner: CommandRunner | None = None,
    sat_model_path: str,
    glotlid_model_path: str | None = None,
    queue: str | None = None,
) -> None:
    """Plan a submission, and perform it only behind the apply gate."""
    snapshot = read_snapshot(run_dir)
    prepared = _reuse_verified_staged_job(
        run_dir,
        snapshot,
        shard,
        remote_project_dir=remote_project_dir,
        remote_source_dir=remote_source_dir,
        remote_run_dir=remote_run_dir,
        processing_seconds=processing_seconds,
        batch_size=batch_size,
        walltime_seconds=walltime_seconds,
        sat_model_path=sat_model_path,
        glotlid_model_path=glotlid_model_path,
    )
    if prepared is None:
        prepared = prepare_job(
            run_dir,
            snapshot,
            shard,
            remote_project_dir=remote_project_dir,
            remote_source_dir=remote_source_dir,
            remote_run_dir=remote_run_dir,
            processing_seconds=processing_seconds,
            batch_size=batch_size,
            walltime_seconds=walltime_seconds,
            sat_model_path=sat_model_path,
            glotlid_model_path=glotlid_model_path,
            remote_bundle_dir=None,
        )
    bundle, paths = prepared
    command_runner = runner or grid_command_runner
    if apply:
        usage_output, quota_output, account_job_count, captured_at = _capture_policy(
            site, runner=command_runner
        )
    else:
        usage_output, quota_output = None, None
        account_job_count, captured_at = None, None
    # Evaluate no earlier than the evidence was captured, or it reads as future.
    moment = _utc_now()
    verdict = evaluate_policy(
        moment=moment,
        usage_policy_output=usage_output,
        home_quota_output=quota_output,
        walltime_seconds=walltime_seconds,
        cores=1,
        allow_daytime=allow_daytime,
        account_job_count=account_job_count,
        evidence_captured_at=captured_at,
        require_fresh_evidence=apply,
    )
    plan, result = submit_job(
        paths,
        bundle,
        policy=verdict,
        allowed_root=run_dir,
        apply=apply,
        walltime_seconds=walltime_seconds,
        require_fresh_policy=apply,
        runner=command_runner,
        now=moment,
        queue=queue,
    )
    print_json(
        {
            "applied": result is not None,
            "plan": plan.to_payload(),
            "result": None if result is None else result.to_payload(),
        }
    )


def _reuse_verified_staged_job(
    run_dir: Path,
    snapshot: SnapshotManifest,
    shard: str,
    *,
    remote_project_dir: str,
    remote_source_dir: str,
    remote_run_dir: str,
    processing_seconds: int,
    batch_size: int,
    walltime_seconds: int,
    sat_model_path: str,
    glotlid_model_path: str | None = None,
) -> tuple[JobBundle, JobPaths] | None:
    """Re-prepare a staged job so every requested setting remains immutable."""
    if not jobs_root(run_dir).exists():
        return None
    bundle = bundle_for_shard(snapshot, shard)
    paths = job_paths(run_dir, bundle)
    for payload in _portable_payload_candidates(paths):
        verify_prepared_bundle(payload, expected_bundle=bundle)
        prepared = prepare_job(
            run_dir,
            snapshot,
            shard,
            remote_project_dir=remote_project_dir,
            remote_source_dir=remote_source_dir,
            remote_run_dir=remote_run_dir,
            processing_seconds=processing_seconds,
            batch_size=batch_size,
            walltime_seconds=walltime_seconds,
            sat_model_path=sat_model_path,
            glotlid_model_path=glotlid_model_path,
            remote_bundle_dir=_infer_remote_bundle_dir(
                remote_project_dir, remote_source_dir, remote_run_dir
            ),
        )
        _verify_local_staged_files(prepared[1], payload)
        return prepared
    return None


def _portable_payload_candidates(paths: JobPaths) -> tuple[Path, ...]:
    root = _existing_job_directory(paths.root)
    if root is None:
        return ()
    return tuple(
        candidate
        for candidate in sorted(root.iterdir())
        if _is_portable_payload(candidate.name, paths.payload_root.name)
    )


def _is_portable_payload(name: str, stable_name: str) -> bool:
    return name == stable_name or name.startswith("payload-")


def _infer_remote_bundle_dir(
    remote_project_dir: str, remote_source_dir: str, remote_run_dir: str
) -> str:
    """Infer a portable root only from the validated project/source/run siblings."""
    siblings = (
        ("project", Path(remote_project_dir)),
        ("source", Path(remote_source_dir)),
        ("run", Path(remote_run_dir)),
    )
    # Any sibling's parent works: when the three disagree the check below refuses
    # whichever parent was chosen, and when they agree all three parents are equal.
    parent = siblings[0][1].parent  # pragma: no mutate - equal or refused either way
    if any(path.parent != parent or path.name != name for name, path in siblings):
        raise GridOperatorError(
            "staged portable submission requires sibling remote project/source/run paths"
        )
    return str(parent)


def _existing_job_directory(root: Path) -> Path | None:
    if root.is_symlink():
        raise GridOperatorError(f"job directory must not be a symlink: {root}")
    if not root.exists():
        return None
    if not root.is_dir():
        raise GridOperatorError(f"job directory is not a regular directory: {root}")
    return root


def _verify_local_staged_files(paths: JobPaths, payload: Path) -> None:
    for local in (paths.bundle, paths.config, paths.script):
        staged = payload / local.name
        if local.is_symlink() or not local.is_file() or local.read_bytes() != staged.read_bytes():
            raise GridOperatorError(f"local prepared artifact differs from staged payload: {local}")


def handle_grid_status(run_dir: Path, shard: str, apply: bool) -> None:
    """Reconcile a recorded submission before anything else is attempted."""
    bundle = bundle_for_shard(read_snapshot(run_dir), shard)
    paths = job_paths(run_dir, bundle)
    if apply:
        reconciliation = reconcile_job(
            paths,
            apply=True,
            resolve_job_name=resolve_job_name,
        )
    else:
        reconciliation = reconcile_job(paths, apply=apply)
    print_json({"bundle_id": bundle.bundle_id, "shard": shard, **reconciliation.to_payload()})


def handle_grid_stage(
    *,
    run_dir: Path,
    project_root: Path,
    source_root: Path,
    shard: str,
    remote_bundle_dir: str,
    processing_seconds: int,
    batch_size: int,
    walltime_seconds: int,
    apply: bool,
    sat_model_path: str,
    glotlid_model_path: str | None = None,
    runner: CommandRunner | None = None,
) -> None:
    """Prepare a portable bundle and transfer it only behind the apply gate."""
    snapshot = read_snapshot(run_dir)
    quarantined = quarantine_orphan_artifacts(run_dir, shard)
    prepared = prepare_portable_job(
        run_dir,
        project_root,
        source_root,
        snapshot,
        shard,
        remote_bundle_dir=remote_bundle_dir,
        processing_seconds=processing_seconds,
        batch_size=batch_size,
        walltime_seconds=walltime_seconds,
        sat_model_path=sat_model_path,
        glotlid_model_path=glotlid_model_path,
    )
    transfer_argv = build_bundle_transfer_argv(prepared, remote_bundle_dir)
    remote_paths = _portable_remote_paths(remote_bundle_dir)
    result = _execute_transport(transfer_argv, runner) if apply else None
    print_json(
        {
            "applied": result is not None,
            "bundle_id": prepared.bundle.bundle_id,
            "input_row_count": prepared.bundle.input_row_count,
            "manifest": str(prepared.manifest),
            "payload_dir": str(prepared.payload_root),
            "processing_seconds": processing_seconds,
            "quarantined": list(quarantined),
            "batch_size": batch_size,
            "walltime_seconds": walltime_seconds,
            "run_dir": str(run_dir),
            **remote_paths,
            "shard": prepared.bundle.shard,
            "transfer_argv": list(transfer_argv),
            "transfer_result": _transport_payload(transfer_argv, result),
        }
    )


def _acknowledge_imported_results(
    run_dir: Path, shard: str, report: RunReport, retrieved_run_dir: Path | None = None
) -> SubmissionIntent:
    bundle = bundle_for_shard(read_snapshot(run_dir), shard)
    paths = job_paths(run_dir, bundle)
    if retrieved_run_dir is not None:
        # The job was submitted from the site's frontend against the site's own
        # copy of the run, so the durable intent lives there; adopt it before
        # acknowledging, or there is nothing here to acknowledge against.
        adopt_retrieved_intent(paths, retrieved_run_dir, bundle)
    return acknowledge_collected_results(paths, report)


def handle_grid_collect(
    run_dir: Path,
    shard: str,
    remote_bundle_dir: str | None = None,
    retrieved_run_dir: Path | None = None,
    apply: bool = False,
    *,
    runner: CommandRunner | None = None,
) -> None:
    """Validate local results, or retrieve, import, and acknowledge a run."""
    if remote_bundle_dir is None:
        if retrieved_run_dir is None:
            _emit_local_collection(run_dir, shard)
        else:
            _import_and_acknowledge_collection(run_dir, shard, retrieved_run_dir)
        return
    _retrieve_and_collect(
        run_dir,
        shard,
        remote_bundle_dir,
        _require_retrieved_run_dir(retrieved_run_dir),
        apply,
        runner,
    )


def _emit_local_collection(run_dir: Path, shard: str) -> None:
    report = collect_results(run_dir, shard)
    print_json({"run_dir": str(run_dir), **report.to_payload()})


def _require_retrieved_run_dir(retrieved_run_dir: Path | None) -> Path:
    if retrieved_run_dir is None:
        raise GridOperatorError(
            "remote collection requires --retrieved-run-dir for its local staging area"
        )
    return retrieved_run_dir


def _import_and_acknowledge_collection(run_dir: Path, shard: str, retrieved_run_dir: Path) -> None:
    report = import_retrieved_results(run_dir, retrieved_run_dir, shard)
    acknowledgment = _acknowledge_imported_results(run_dir, shard, report, retrieved_run_dir)
    print_json(
        {
            **report.to_payload(),
            "acknowledgment": acknowledgment.to_payload(),
            "applied": False,
            "retrieved_run_dir": str(retrieved_run_dir),
            "run_dir": str(run_dir),
            "shard": shard,
            "transfer_argv": None,
            "transfer_result": None,
        }
    )


def _retrieve_and_collect(
    run_dir: Path,
    shard: str,
    remote_bundle_dir: str,
    retrieved_run_dir: Path,
    apply: bool,
    runner: CommandRunner | None,
) -> None:
    remote_run_dir = _remote_child(remote_bundle_dir.rstrip("/") or "/", "run")
    transfer_argv = build_result_retrieval_argv(remote_run_dir, retrieved_run_dir, shard)
    if not apply:
        _emit_retrieval_plan(run_dir, shard, retrieved_run_dir, transfer_argv)
        return
    _seed_retrieval_snapshot(run_dir, retrieved_run_dir)
    result = _execute_transport(transfer_argv, runner)
    report = import_retrieved_results(run_dir, retrieved_run_dir, shard)
    acknowledgment = _acknowledge_imported_results(run_dir, shard, report, retrieved_run_dir)
    print_json(
        {
            **report.to_payload(),
            "acknowledgment": acknowledgment.to_payload(),
            "applied": True,
            "retrieved_run_dir": str(retrieved_run_dir),
            "run_dir": str(run_dir),
            "shard": shard,
            "transfer_argv": list(transfer_argv),
            "transfer_result": _transport_payload(transfer_argv, result),
        }
    )


def _emit_retrieval_plan(
    run_dir: Path, shard: str, retrieved_run_dir: Path, transfer_argv: Sequence[str]
) -> None:
    print_json(
        {
            "acknowledgment": None,
            "applied": False,
            "report": None,
            "retrieved_run_dir": str(retrieved_run_dir),
            "run_dir": str(run_dir),
            "shard": shard,
            "transfer_argv": list(transfer_argv),
            "transfer_result": None,
        }
    )
