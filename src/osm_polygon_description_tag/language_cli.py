"""The ``language`` command group for local description language detection.

Preparation, processing, and validation are separate commands so that freezing
an input snapshot, spending a bounded processing budget, and auditing what was
produced are all independently repeatable. Every successful command writes one
JSON document to stdout; diagnostics stay on stderr.
"""

import sys
from pathlib import Path
from types import ModuleType
from typing import Annotated

import typer

from osm_polygon_description_tag import grid_transport as _grid_transport_module
from osm_polygon_description_tag import grid_workflow as _grid_workflow_module
from osm_polygon_description_tag import language_workflow as _language_workflow_module
from osm_polygon_description_tag import publication_workflow as _publication_workflow_module
from osm_polygon_description_tag.dataset.languages.models import cascade_model_identity
from osm_polygon_description_tag.dataset.languages.worker import (
    DEFAULT_BATCH_SIZE,
    DEFAULT_BUDGET_SECONDS,
)
from osm_polygon_description_tag.grid_workflow import (
    handle_grid_collect,
    handle_grid_prepare,
    handle_grid_stage,
    handle_grid_status,
    handle_grid_submit,
)
from osm_polygon_description_tag.language_workflow import (
    _policy,
    handle_prepare,
    handle_run,
    handle_validate,
)
from osm_polygon_description_tag.publication_workflow import handle_export, handle_publish
from osm_polygon_description_tag.workflow.grid_policy import (
    MAX_PROCESSING_SECONDS,
    MAX_WALLTIME_SECONDS,
)

_WORKFLOW_MODULES = (
    _language_workflow_module,
    _grid_transport_module,
    _grid_workflow_module,
    _publication_workflow_module,
)


class _LanguageCliModule(ModuleType):
    """Preserve the historical monkeypatch seam while handlers live elsewhere."""

    def __setattr__(self, name: str, value: object) -> None:
        for module in _WORKFLOW_MODULES:
            if name in vars(module):
                setattr(module, name, value)
        super().__setattr__(name, value)


def __getattr__(name: str) -> object:
    """Resolve compatibility imports from the focused workflow modules."""
    for module in _WORKFLOW_MODULES:
        try:
            return getattr(module, name)
        except AttributeError:
            continue
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")


sys.modules[__name__].__class__ = _LanguageCliModule

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
