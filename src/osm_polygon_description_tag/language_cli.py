"""The ``language`` command group for local description language detection.

Preparation, processing, and validation are separate commands so that freezing
an input snapshot, spending a bounded processing budget, and auditing what was
produced are all independently repeatable. Every successful command writes one
JSON document to stdout; diagnostics stay on stderr.
"""

import shutil
from collections.abc import Sequence
from datetime import UTC, datetime
from pathlib import Path
from typing import Annotated

import typer

from osm_polygon_description_tag.dataset.languages.checkpoint import exclusive_worker_lock
from osm_polygon_description_tag.dataset.languages.detector import (
    FallbackLanguageDetector,
    LanguageDetector,
    build_language_detector,
    build_lingua_detector,
)
from osm_polygon_description_tag.dataset.languages.models import (
    CASCADE_DETECTOR_NAME,
    DEFAULT_LANGUAGE_POLICY,
    DEFAULT_LANGUAGE_SCOPE,
    LINGUA_DETECTOR_NAME,
    LanguageModelIdentity,
    LanguagePolicy,
    cascade_model_identity,
)
from osm_polygon_description_tag.dataset.languages.snapshot import (
    SnapshotError,
    SnapshotManifest,
    fingerprint_lockfile,
    fingerprint_project_source,
    prepare_snapshot,
    read_snapshot,
    verify_project_identity,
)
from osm_polygon_description_tag.dataset.languages.validation import RunReport, validate_run
from osm_polygon_description_tag.dataset.languages.worker import (
    DEFAULT_BATCH_SIZE,
    DEFAULT_BUDGET_SECONDS,
    ProcessingBudget,
    process_shard,
)
from osm_polygon_description_tag.dataset.sentences.sat import build_sat_splitter
from osm_polygon_description_tag.publication.language import (
    LanguagePublicationError,
    build_language_upload_plan,
    export_language_annotations,
    language_config_yaml,
    read_language_export,
    render_language_card_section,
)
from osm_polygon_description_tag.publication.language_hub import build_language_hub
from osm_polygon_description_tag.publication.language_upload import (
    PUBLICATION_STATE_FILENAME,
    publish_language_export,
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
from osm_polygon_description_tag.workflow.grid_policy import (
    MAX_PROCESSING_SECONDS,
    MAX_WALLTIME_SECONDS,
    evaluate_policy,
)
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

language_app = typer.Typer(
    name="language",
    help="Prepare, run, and validate description language detection",
    add_completion=False,
    no_args_is_help=True,
    pretty_exceptions_enable=False,
)

SourceRoot = Annotated[Path, typer.Option("--source-root", help="Immutable source Parquet root")]
RunDir = Annotated[Path, typer.Option("--run-dir", help="Owned run directory for this snapshot")]
ProjectRoot = Annotated[
    Path, typer.Option("--project-root", help="Project root providing src/ and uv.lock")
]
Shard = Annotated[str, typer.Option("--shard", help="Snapshot-relative source Parquet path")]
BatchSize = Annotated[int, typer.Option("--batch-size", help="Input rows per committed batch")]
BudgetSeconds = Annotated[
    float, typer.Option("--budget-seconds", help="Monotonic processing budget for this attempt")
]
MinChars = Annotated[
    int | None,
    typer.Option("--min-alphabetic-chars", help="Minimum letters before detection is attempted"),
]
PolicyVersion = Annotated[str, typer.Option("--policy-version", help="Named policy preset: v1")]

GlotLIDModelPath = Annotated[
    Path | None, typer.Option("--glotlid-model-path", help="Pinned GlotLID v3 model file")
]

SatModelPath = Annotated[
    Path,
    typer.Option(
        "--sat-model-path",
        help="Directory holding the pinned SaT-3l-sm weights, config, and tokenizer",
    ),
]

RemoteSatModelPath = Annotated[
    str,
    typer.Option("--sat-model-path", help="Remote directory holding the pinned SaT-3l-sm model"),
]

_POLICY_PRESETS = {
    "v1": DEFAULT_LANGUAGE_POLICY,
}


def _policy(
    min_alphabetic_chars: int | None,
    *,
    policy_version: str = "v1",
) -> LanguagePolicy:
    try:
        base = _POLICY_PRESETS[policy_version]
    except KeyError:
        raise ValueError("policy_version must be 'v1'") from None
    return LanguagePolicy(
        min_alphabetic_chars=(
            base.min_alphabetic_chars if min_alphabetic_chars is None else min_alphabetic_chars
        ),
    )


def _prepare_identity(
    policy: LanguagePolicy, model_identity: LanguageModelIdentity | None
) -> LanguageModelIdentity:
    identity = model_identity or cascade_model_identity(policy)
    if not isinstance(identity, LanguageModelIdentity):
        raise TypeError("model_identity must be a LanguageModelIdentity")
    if identity.policy != policy:
        raise SnapshotError("model identity policy does not match the requested policy")
    return identity


def _prepare_report(
    source_root: Path, run_dir: Path, snapshot: SnapshotManifest
) -> dict[str, object]:
    return {
        "snapshot_id": snapshot.snapshot_id,
        "run_dir": str(run_dir),
        "source_root": str(source_root),
        "source_file_count": len(snapshot.source_files),
        "input_row_count": sum(item.row_count for item in snapshot.source_files),
        "model_config_fingerprint": snapshot.model_config_fingerprint,
        "detector_name": snapshot.model_identity.detector_name,
        "library_name": snapshot.model_identity.library_name,
        "library_version": snapshot.model_identity.library_version,
        "shards": [item.relative_path for item in snapshot.source_files],
    }


def handle_prepare(
    source_root: Path,
    run_dir: Path,
    project_root: Path,
    policy: LanguagePolicy,
    *,
    model_identity: LanguageModelIdentity | None = None,
) -> SnapshotManifest:
    """Freeze the immutable input snapshot for one run directory."""
    identity = _prepare_identity(policy, model_identity)
    snapshot = prepare_snapshot(
        source_root,
        run_dir,
        code_fingerprint=fingerprint_project_source(project_root),
        lock_fingerprint=fingerprint_lockfile(project_root),
        model_identity=identity,
        policy=policy,
    )
    print_json(_prepare_report(source_root, run_dir, snapshot))
    return snapshot


def handle_run(
    source_root: Path,
    run_dir: Path,
    shard: str,
    batch_size: int,
    budget_seconds: float,
    project_root: Path = Path(),
    *,
    sat_model_path: Path,
    glotlid_model_path: Path | None = None,
) -> None:
    """Process one staged shard within a bounded monotonic budget.

    The run directory is locked for the lifetime of the attempt so a second
    local worker cannot interleave commits into the same checkpoints.
    """
    snapshot = read_snapshot(run_dir)
    with exclusive_worker_lock(run_dir):
        verify_project_identity(snapshot, project_root)
        detector = _build_detector_for_snapshot(
            snapshot.model_identity,
            glotlid_model_path=glotlid_model_path,
        )
        if detector.identity.config_fingerprint != snapshot.model_config_fingerprint:
            raise SnapshotError("detector configuration does not match the immutable snapshot")
        outcome = process_shard(
            run_dir,
            source_root,
            shard,
            detector=detector,
            splitter=build_sat_splitter(model_dir=sat_model_path),
            snapshot=snapshot,
            batch_size=batch_size,
            budget=ProcessingBudget(budget_seconds),
        )
    print_json(
        {
            "snapshot_id": snapshot.snapshot_id,
            "shard": outcome.shard,
            "status": str(outcome.status),
            "complete": outcome.is_complete,
            "resumed_from": outcome.resumed_from,
            "input_cursor": outcome.input_cursor,
            "input_row_count": outcome.input_row_count,
            "annotation_count": outcome.annotation_count,
            "part_count": len(outcome.completed_parts),
        }
    )


def handle_validate(run_dir: Path, shard: str | None) -> None:
    """Report run completeness without modifying anything."""
    report = validate_run(run_dir, shards=None if shard is None else (shard,))
    print_json({"run_dir": str(run_dir), **report.to_payload()})


def _build_detector_for_snapshot(
    identity: LanguageModelIdentity,
    *,
    glotlid_model_path: Path | None,
) -> LanguageDetector | FallbackLanguageDetector:
    scope = identity.language_scope
    language_codes = None if scope == DEFAULT_LANGUAGE_SCOPE else scope
    if identity.detector_name == CASCADE_DETECTOR_NAME:
        return build_language_detector(
            identity.policy,
            language_codes=language_codes,
            glotlid_model_path=glotlid_model_path,
        )
    if identity.detector_name == LINGUA_DETECTOR_NAME:
        if glotlid_model_path is not None:
            raise SnapshotError("GlotLID model path requires a cascade snapshot")
        return build_lingua_detector(identity.policy, language_codes=language_codes)
    raise SnapshotError(f"unsupported detector in snapshot: {identity.detector_name!r}")


@language_app.command("prepare", help="Freeze an immutable input snapshot")
def prepare_command(
    source_root: SourceRoot,
    run_dir: RunDir,
    project_root: ProjectRoot = Path(),
    min_alphabetic_chars: MinChars = None,
    policy_version: PolicyVersion = "v1",
) -> None:
    policy = _policy(min_alphabetic_chars, policy_version=policy_version)
    handle_prepare(
        source_root,
        run_dir,
        project_root,
        policy,
        model_identity=cascade_model_identity(policy),
    )


@language_app.command("run", help="Process one shard within a bounded budget")
def run_command(
    source_root: SourceRoot,
    run_dir: RunDir,
    shard: Shard,
    batch_size: BatchSize = DEFAULT_BATCH_SIZE,
    budget_seconds: BudgetSeconds = DEFAULT_BUDGET_SECONDS,
    *,
    sat_model_path: SatModelPath,
    project_root: ProjectRoot = Path(),
    glotlid_model_path: GlotLIDModelPath = None,
) -> None:
    handle_run(
        source_root,
        run_dir,
        shard,
        batch_size,
        budget_seconds,
        project_root,
        sat_model_path=sat_model_path,
        glotlid_model_path=glotlid_model_path,
    )


@language_app.command("validate", help="Report run completeness read-only")
def validate_command(
    run_dir: RunDir,
    shard: Annotated[
        str | None, typer.Option("--shard", help="Restrict the report to one shard")
    ] = None,
) -> None:
    handle_validate(run_dir, shard)


grid_app = typer.Typer(
    name="grid",
    help="Prepare, stage, submit, reconcile, and collect one bounded Grid'5000 job",
    add_completion=False,
    no_args_is_help=True,
    pretty_exceptions_enable=False,
)
language_app.add_typer(grid_app, name="grid")

RemoteProject = Annotated[
    str, typer.Option("--remote-project-dir", help="Absolute project path on the compute node")
]
RemoteSource = Annotated[
    str, typer.Option("--remote-source-dir", help="Absolute staged source path on the node")
]
RemoteRun = Annotated[
    str, typer.Option("--remote-run-dir", help="Absolute staged run path on the node")
]
RemoteBundle = Annotated[
    str, typer.Option("--remote-bundle-dir", help="Absolute remote root for a portable bundle")
]
RemoteGlotLIDModelPath = Annotated[
    str | None, typer.Option("--glotlid-model-path", help="Absolute pinned GlotLID v3 model path")
]
RetrievedRun = Annotated[
    Path | None,
    typer.Option("--retrieved-run-dir", help="Local directory receiving a retrieved run"),
]
Site = Annotated[str, typer.Option("--site", help="Grid'5000 site to check policy against")]
Apply = Annotated[
    bool,
    typer.Option(
        "--apply", help="Actually perform external transport or scheduler work instead of planning"
    ),
]
AllowDaytime = Annotated[
    bool,
    typer.Option(
        "--allow-daytime",
        help="Permit weekday daytime submission after verifying your own accounting",
    ),
]
ProcessingSeconds = Annotated[
    int, typer.Option("--processing-seconds", help="Useful processing budget inside the job")
]
Walltime = Annotated[int, typer.Option("--walltime-seconds", help="Requested OAR walltime")]


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


def _remote_child(base: str, name: str) -> str:
    return f"/{name}" if base == "/" else f"{base}/{name}"


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


@grid_app.command("prepare", help="Write the immutable job bundle and script")
def grid_prepare_command(
    run_dir: RunDir,
    shard: Shard,
    remote_project_dir: RemoteProject,
    remote_source_dir: RemoteSource,
    remote_run_dir: RemoteRun,
    *,
    sat_model_path: RemoteSatModelPath,
    processing_seconds: ProcessingSeconds = MAX_PROCESSING_SECONDS,
    batch_size: BatchSize = DEFAULT_BATCH_SIZE,
    glotlid_model_path: RemoteGlotLIDModelPath = None,
) -> None:
    handle_grid_prepare(
        run_dir,
        shard,
        remote_project_dir,
        remote_source_dir,
        remote_run_dir,
        processing_seconds,
        batch_size,
        sat_model_path=sat_model_path,
        glotlid_model_path=glotlid_model_path,
    )


@grid_app.command("stage", help="Prepare and optionally transfer a portable bundle")
def grid_stage_command(
    run_dir: RunDir,
    shard: Shard,
    project_root: ProjectRoot,
    source_root: SourceRoot,
    remote_bundle_dir: RemoteBundle,
    processing_seconds: ProcessingSeconds = MAX_PROCESSING_SECONDS,
    batch_size: BatchSize = DEFAULT_BATCH_SIZE,
    walltime_seconds: Walltime = MAX_WALLTIME_SECONDS,
    glotlid_model_path: RemoteGlotLIDModelPath = None,
    apply: Apply = False,
    *,
    sat_model_path: RemoteSatModelPath,
) -> None:
    handle_grid_stage(
        run_dir=run_dir,
        project_root=project_root,
        source_root=source_root,
        shard=shard,
        remote_bundle_dir=remote_bundle_dir,
        processing_seconds=processing_seconds,
        batch_size=batch_size,
        walltime_seconds=walltime_seconds,
        sat_model_path=sat_model_path,
        glotlid_model_path=glotlid_model_path,
        apply=apply,
    )


@grid_app.command("submit", help="Plan a submission; the apply gate contacts OAR")
def grid_submit_command(
    run_dir: RunDir,
    shard: Shard,
    remote_project_dir: RemoteProject,
    remote_source_dir: RemoteSource,
    remote_run_dir: RemoteRun,
    site: Site,
    walltime_seconds: Walltime = MAX_WALLTIME_SECONDS,
    processing_seconds: ProcessingSeconds = MAX_PROCESSING_SECONDS,
    batch_size: BatchSize = DEFAULT_BATCH_SIZE,
    allow_daytime: AllowDaytime = False,
    apply: Apply = False,
    glotlid_model_path: RemoteGlotLIDModelPath = None,
    queue: Annotated[
        str | None,
        typer.Option(
            "--queue",
            help="Scheduler queue; sites disagree on what a bare submission means",
        ),
    ] = None,
    *,
    sat_model_path: RemoteSatModelPath,
) -> None:
    handle_grid_submit(
        run_dir,
        shard,
        remote_project_dir,
        remote_source_dir,
        remote_run_dir,
        site,
        walltime_seconds,
        processing_seconds,
        batch_size,
        allow_daytime,
        apply,
        sat_model_path=sat_model_path,
        glotlid_model_path=glotlid_model_path,
        queue=queue,
    )


@grid_app.command("status", help="Reconcile a recorded submission")
def grid_status_command(run_dir: RunDir, shard: Shard, apply: Apply = False) -> None:
    handle_grid_status(run_dir, shard, apply)


@grid_app.command("collect", help="Retrieve, validate, import, and acknowledge job results")
def grid_collect_command(
    run_dir: RunDir,
    shard: Shard,
    remote_bundle_dir: Annotated[
        str | None, typer.Option("--remote-bundle-dir", help="Remote bundle root to retrieve from")
    ] = None,
    retrieved_run_dir: RetrievedRun = None,
    apply: Apply = False,
) -> None:
    handle_grid_collect(
        run_dir,
        shard,
        remote_bundle_dir=remote_bundle_dir,
        retrieved_run_dir=retrieved_run_dir,
        apply=apply,
    )


ExportDir = Annotated[
    Path, typer.Option("--export-dir", help="Owned directory for the additive export")
]
CardSection = Annotated[
    Path | None,
    typer.Option("--card-section", help="Write the generated dataset-card section here"),
]
Repo = Annotated[str, typer.Option("--repo", help="Target Hugging Face dataset repository")]
ConfirmRepo = Annotated[
    str, typer.Option("--confirm-repo", help="Repeat the repository to confirm the target")
]
BaselineRevision = Annotated[
    str | None,
    typer.Option("--baseline-revision", help="Revision the plan was built against"),
]


def handle_export(run_dir: Path, export_dir: Path, card_section: Path | None) -> None:
    """Export one complete run into the additive language-v1 tree."""
    export = export_language_annotations(run_dir, export_dir)
    if card_section is not None:
        card_section.parent.mkdir(parents=True, exist_ok=True)
        section = render_language_card_section(export)
        card_section.write_text(section, encoding="utf-8")  # pragma: no mutate - codec alias only
    print_json(
        {
            "export_dir": str(export_dir),
            "card_section": None if card_section is None else str(card_section),
            "config_yaml": language_config_yaml(),
            **export.to_payload(),
        }
    )


def handle_publish(
    export_dir: Path,
    repo: str,
    confirm_repo: str,
    baseline_revision: str | None,
    apply: bool,
) -> None:
    """Plan the additive publication, and perform it only behind the apply gate."""
    export = read_language_export(export_dir)
    plan = build_language_upload_plan(export, repo, confirm_repo=confirm_repo)
    hub = build_language_hub()
    if apply and baseline_revision is None:
        raise LanguagePublicationError(
            "applying a publication requires the baseline revision the plan was built against"
        )
    baseline = baseline_revision or hub.repo_revision(repo)
    outcome = publish_language_export(
        plan,
        hub,
        baseline_revision=baseline,
        apply=apply,
        state_path=export_dir / PUBLICATION_STATE_FILENAME,
    )
    print_json(
        {
            "repo_id": repo,
            "plan_identity_sha256": plan.identity_sha256,
            "planned_files": [item.relative_path for item in plan.files],
            "baseline_revision": baseline,
            **outcome.to_payload(),
        }
    )


@language_app.command("export", help="Export a complete run as additive language-v1 files")
def export_command(
    run_dir: RunDir,
    export_dir: ExportDir,
    card_section: CardSection = None,
) -> None:
    handle_export(run_dir, export_dir, card_section)


@language_app.command("publish", help="Plan the additive upload; the apply gate uploads")
def publish_command(
    export_dir: ExportDir,
    repo: Repo,
    confirm_repo: ConfirmRepo,
    baseline_revision: BaselineRevision = None,
    apply: Apply = False,
) -> None:
    handle_publish(export_dir, repo, confirm_repo, baseline_revision, apply)


__all__ = [
    "grid_app",
    "handle_export",
    "handle_grid_collect",
    "handle_grid_prepare",
    "handle_grid_stage",
    "handle_grid_status",
    "handle_grid_submit",
    "handle_prepare",
    "handle_publish",
    "handle_run",
    "handle_validate",
    "language_app",
]
