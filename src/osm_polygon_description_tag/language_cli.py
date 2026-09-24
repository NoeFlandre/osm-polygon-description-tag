"""The ``language`` command group for local description language detection.

Preparation, processing, and validation are separate commands so that freezing
an input snapshot, spending a bounded processing budget, and auditing what was
produced are all independently repeatable. Every successful command writes one
JSON document to stdout; diagnostics stay on stderr.
"""

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
    V2_LANGUAGE_POLICY,
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
from osm_polygon_description_tag.dataset.languages.validation import validate_run
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
from osm_polygon_description_tag.workflow.grid_commands import (
    handle_grid_collect,
    handle_grid_prepare,
    handle_grid_stage,
    handle_grid_status,
    handle_grid_submit,
)
from osm_polygon_description_tag.workflow.grid_policy import (
    MAX_PROCESSING_SECONDS,
    MAX_WALLTIME_SECONDS,
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
ReportShard = Annotated[
    str | None, typer.Option("--shard", help="Restrict the report to one shard")
]
BatchSize = Annotated[int, typer.Option("--batch-size", help="Input rows per committed batch")]
BudgetSeconds = Annotated[
    float, typer.Option("--budget-seconds", help="Monotonic processing budget for this attempt")
]
MinChars = Annotated[
    int | None,
    typer.Option("--min-alphabetic-chars", help="Minimum letters before detection is attempted"),
]
MinScore = Annotated[
    float | None, typer.Option("--min-score", help="Minimum raw top score to accept")
]
MinMargin = Annotated[
    float | None, typer.Option("--min-margin", help="Minimum raw score margin over the runner-up")
]
PolicyVersion = Annotated[
    str, typer.Option("--policy-version", help="Named policy preset: v1 or v2")
]

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
    "v2": V2_LANGUAGE_POLICY,
}


def _policy(
    min_alphabetic_chars: int | None,
    min_score: float | None,
    min_margin: float | None,
    *,
    policy_version: str = "v1",
) -> LanguagePolicy:
    try:
        base = _POLICY_PRESETS[policy_version]
    except KeyError:
        raise ValueError("policy_version must be 'v1' or 'v2'") from None
    return LanguagePolicy(
        min_alphabetic_chars=(
            base.min_alphabetic_chars if min_alphabetic_chars is None else min_alphabetic_chars
        ),
        min_score=base.min_score if min_score is None else min_score,
        min_margin=base.min_margin if min_margin is None else min_margin,
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
    min_score: MinScore = None,
    min_margin: MinMargin = None,
    policy_version: PolicyVersion = "v1",
) -> None:
    policy = _policy(
        min_alphabetic_chars,
        min_score,
        min_margin,
        policy_version=policy_version,
    )
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
    shard: ReportShard = None,
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
RetrievalBundle = Annotated[
    str | None, typer.Option("--remote-bundle-dir", help="Remote bundle root to retrieve from")
]
Queue = Annotated[
    str | None,
    typer.Option(
        "--queue",
        help="Scheduler queue; sites disagree on what a bare submission means",
    ),
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
    queue: Queue = None,
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
    remote_bundle_dir: RetrievalBundle = None,
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
