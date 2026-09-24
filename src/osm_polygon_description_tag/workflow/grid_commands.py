"""Grid'5000 command handlers behind the ``language grid`` console commands.

Each handler turns validated command-line values into one operator call and
prints exactly one JSON document; the Typer wiring stays in ``language_cli``.
"""

import shutil
from collections.abc import Sequence
from datetime import UTC, datetime
from pathlib import Path

from osm_polygon_description_tag.dataset.languages.snapshot import SnapshotManifest, read_snapshot
from osm_polygon_description_tag.dataset.languages.validation import RunReport
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
    gather_policy_evidence,
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
from osm_polygon_description_tag.workflow.grid_operator import (
    remote_child as _remote_child,
)
from osm_polygon_description_tag.workflow.grid_policy import evaluate_policy
from osm_polygon_description_tag.workflow.grid_scheduler import (
    DEFAULT_COMMAND_TIMEOUT,
    CommandResult,
    CommandRunner,
    SchedulerError,
    account_active_job_count,
)
from osm_polygon_description_tag.workflow.grid_scheduler import (
    run_command as grid_command_runner,
)


def _execute_transport(argv: Sequence[str], runner: CommandRunner | None) -> CommandResult:
    """Run one explicit transport argv only after its caller opened ``--apply``."""
    command_runner = runner or grid_command_runner
    try:
        result = command_runner(argv, DEFAULT_COMMAND_TIMEOUT)
    except SchedulerError as error:
        raise GridOperatorError(f"transport command could not run: {error}") from error
    _raise_transport_failure(result)
    return result


def _raise_transport_failure(result: CommandResult) -> None:
    if result.timed_out:
        raise GridOperatorError("transport command timed out")
    if result.returncode != 0:
        detail = result.stderr.strip() or "no diagnostic"
        raise GridOperatorError(f"transport command exited {result.returncode}: {detail}")


def _transport_payload(
    argv: Sequence[str], result: CommandResult | None
) -> dict[str, object] | None:
    if result is None:
        return None
    return {
        "argv": list(argv),
        "returncode": result.returncode,
        "stderr": result.stderr,
        "stdout": result.stdout,
        "timed_out": result.timed_out,
    }


def _utc_now() -> datetime:
    """Return the current instant.

    This is the one clock the Grid handlers read, so a test can freeze it and
    evaluate the Europe/Paris day/night policy at a chosen moment.
    """
    return datetime.now(UTC)


def _capture_policy(
    site: str, *, runner: CommandRunner
) -> tuple[str | None, str | None, int | None, datetime]:
    """Capture all policy inputs and count only active account-wide jobs."""
    captured_at = _utc_now()
    usage_output, quota_output, account_job_output = gather_policy_evidence(site, runner=runner)
    if account_job_output is None:
        account_job_count = None
    else:
        try:
            account_job_count = account_active_job_count(account_job_output)
        except SchedulerError as error:
            raise GridOperatorError(
                f"account-wide scheduler evidence is invalid: {error}"
            ) from error
    return usage_output, quota_output, account_job_count, captured_at


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


def _portable_remote_paths(remote_bundle_dir: str) -> dict[str, str]:
    """Expose the exact remote paths required to reuse a staged job script."""
    base = remote_bundle_dir.rstrip("/") or "/"
    return {
        "remote_bundle_dir": base,
        "remote_project_dir": _remote_child(base, "project"),
        "remote_run_dir": _remote_child(base, "run"),
        "remote_source_dir": _remote_child(base, "source"),
    }


def _seed_retrieval_snapshot(local_run_dir: Path, retrieved_run_dir: Path) -> None:
    """Give a shard-only retrieval the immutable snapshot needed for import."""
    source = _retrieval_snapshot_source(local_run_dir)
    _prepare_retrieval_directory(retrieved_run_dir)
    target = _retrieval_snapshot_target(retrieved_run_dir)
    if target.exists():
        return
    _copy_retrieval_snapshot(source, target)


def _retrieval_snapshot_source(local_run_dir: Path) -> Path:
    source = local_run_dir / "snapshot.json"
    if source.is_symlink() or not source.is_file():
        raise GridOperatorError(f"local run snapshot is missing: {source}")
    return source


def _prepare_retrieval_directory(retrieved_run_dir: Path) -> None:
    if retrieved_run_dir.is_symlink():
        raise GridOperatorError(
            f"retrieved run directory must not be a symlink: {retrieved_run_dir}"
        )
    retrieved_run_dir.mkdir(parents=True, exist_ok=True)


def _retrieval_snapshot_target(retrieved_run_dir: Path) -> Path:
    target = retrieved_run_dir / "snapshot.json"
    if target.is_symlink():
        raise GridOperatorError(f"retrieved snapshot must not be a symlink: {target}")
    return target


def _copy_retrieval_snapshot(source: Path, target: Path) -> None:
    try:
        shutil.copyfile(source, target)
    except OSError as error:
        raise GridOperatorError(f"cannot seed retrieved run snapshot: {error}") from error


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
