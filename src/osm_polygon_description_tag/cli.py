"""Typer CLI for deterministic polygon dataset build and publication.

Successful commands write one JSON document to stdout. Usage diagnostics,
operational events, and domain errors are confined to stderr.
"""

from __future__ import annotations

import os
import sys
import uuid
from collections.abc import Callable, Sequence
from importlib.metadata import version as package_version
from pathlib import Path
from types import SimpleNamespace
from typing import Annotated, Any

import typer
from typer import rich_utils

from osm_polygon_description_tag.dataset.docs import generate_dataset_docs
from osm_polygon_description_tag.dataset.languages.detector import LanguageDetectionError
from osm_polygon_description_tag.dataset.manifest import ManifestError, read_manifest
from osm_polygon_description_tag.dataset.migration import MigrationError, migrate_dataset_schema
from osm_polygon_description_tag.dataset.stats import ReportingError
from osm_polygon_description_tag.dataset.storage import (
    StorageError,
    validate_finalized_artifacts,
    validate_geoparquet,
)
from osm_polygon_description_tag.dataset.text_migration import (
    TextMigrationError,
    migrate_dataset_text,
)
from osm_polygon_description_tag.language_cli import language_app
from osm_polygon_description_tag.observability.trackio import (
    TrackioRecorder,
    publish_snapshot,
)
from osm_polygon_description_tag.osm.discovery import discover_sources
from osm_polygon_description_tag.osm.extraction import OsmiumExportError
from osm_polygon_description_tag.publication import (
    PublicationError,
    create_upload_plan,
    execute_upload,
    release_metadata,
)
from osm_polygon_description_tag.publication.verification import HubVerificationError
from osm_polygon_description_tag.runtime.click_compat import ClickException, Exit, UsageError
from osm_polygon_description_tag.runtime.config import (
    MissingPathError,
    Paths,
    UnsafePathError,
    resolve_data_root,
)
from osm_polygon_description_tag.runtime.logging import RunLogger
from osm_polygon_description_tag.runtime.presentation import TerminalPresenter, print_json
from osm_polygon_description_tag.runtime.resources import (
    dataset_card_template,
    osmium_export_config,
)
from osm_polygon_description_tag.workflow.build import BuildResult, build_all, build_one
from osm_polygon_description_tag.workflow.grid_driver import DriverError
from osm_polygon_description_tag.workflow.orchestrator import (
    OrchestratorError,
    run_and_publish,
)
from osm_polygon_description_tag.workflow.preflight import PreflightError

app = typer.Typer(
    name="osm-polygon-description-tag",
    add_completion=False,
    no_args_is_help=False,
    pretty_exceptions_enable=False,
)
app.add_typer(language_app, name="language")

SourceRoot = Annotated[
    Path | None,
    typer.Option(
        "--source-root",
        help="Immutable PBF directory [default: $OSM_POLYGON_SOURCE_ROOT]. "
        "Only commands that read PBFs use it.",
    ),
]
DataRoot = Annotated[
    Path | None,
    typer.Option(
        "--data-root",
        help="Generated-data directory [default: $OSM_POLYGON_DATA_ROOT].",
    ),
]
Osmium = Annotated[
    str,
    typer.Option("--osmium", help="osmium executable. Only commands that read PBFs use it."),
]

_DISTRIBUTION = "osm-polygon-description-tag"

# stderr threshold for human-readable event lines, set by -v / -q. The JSONL
# log always records every event.
_verbosity = SimpleNamespace(stderr_level="INFO")


def _show_version(value: bool) -> None:
    if value:
        typer.echo(package_version(_DISTRIBUTION))
        raise Exit


@app.callback()
def _global_options(
    version: Annotated[  # noqa: ARG001 - consumed by its eager callback
        bool,
        typer.Option(
            "--version",
            callback=_show_version,
            is_eager=True,
            help="Print the package version and exit.",
        ),
    ] = False,
    verbose: Annotated[
        bool,
        typer.Option(
            "-v", "--verbose", help="Also print DEBUG events, such as the resolved configuration."
        ),
    ] = False,
    quiet: Annotated[
        bool, typer.Option("-q", "--quiet", help="Print only WARNING and ERROR events.")
    ] = False,
) -> None:
    """Build, validate and publish the OSM polygon description-tag dataset."""
    if verbose and quiet:
        raise UsageError("--verbose and --quiet cannot be combined")
    _verbosity.stderr_level = "DEBUG" if verbose else "WARNING" if quiet else "INFO"


def _resolve_paths(args: SimpleNamespace) -> Paths:
    return Paths.resolve(args.source_root, args.data_root)


def _data_root(args: SimpleNamespace) -> Path:
    """Data-only commands need no source root."""
    return resolve_data_root(args.data_root)


class _Interrupted(Exception):  # noqa: N818 - a control-flow signal, not an error
    """Carry Ctrl-C through Typer without its default exit-code conversion."""


def _invoke(handler: Callable[[SimpleNamespace], int], args: SimpleNamespace) -> None:
    try:
        handler(args)
    except KeyboardInterrupt as error:
        raise _Interrupted from error


def handle_inspect(args: SimpleNamespace) -> int:
    paths = _resolve_paths(args)
    sources = discover_sources(paths.source_root)
    print_json(
        {
            "source_root": str(paths.source_root),
            "data_root": str(paths.data_root),
            "osmium_executable": args.osmium,
            "export_config": str(osmium_export_config()),
            "source_count": len(sources),
            "sources": [
                {
                    "name": source.name,
                    "output_name": source.output_name,
                    "size_bytes": source.size_bytes,
                    "mtime_ns": source.mtime_ns,
                }
                for source in sources
            ],
        }
    )
    return 0


def _build_paths_and_executor(
    args: SimpleNamespace,
) -> tuple[Paths, Callable[[Any], BuildResult]]:
    paths = _resolve_paths(args)

    def executor(source: Any) -> BuildResult:
        return build_one(
            source,
            paths,
            export_config=osmium_export_config(),
            executable=args.osmium,
        )

    return paths, executor


def handle_build_one(args: SimpleNamespace) -> int:
    paths, executor = _build_paths_and_executor(args)
    sources = discover_sources(paths.source_root)
    match = next((source for source in sources if source.name == args.basename), None)
    if match is None:
        raise ValueError(f"source not discovered: {args.basename}")
    result = executor(match)
    print_json(
        {
            "source_name": result.source_name,
            "output_name": result.output_name,
            "status": result.status,
            "emitted_features": result.emitted_features,
            "included_rows": result.included_rows,
            "rejections": result.rejections,
            "output_path": str(result.output_path),
            "manifest_path": str(result.manifest_path),
        }
    )
    return 0


def handle_build_all(args: SimpleNamespace) -> int:
    paths, executor = _build_paths_and_executor(args)
    sources = discover_sources(paths.source_root)
    results: list[BuildResult] = build_all(sources, build=executor)
    print_json(
        {
            "count": len(results),
            "results": [
                {
                    "source_name": result.source_name,
                    "status": result.status,
                    "included_rows": result.included_rows,
                }
                for result in results
            ],
        }
    )
    return 0


def handle_validate(args: SimpleNamespace) -> int:
    data_root = _data_root(args)
    data_dir = data_root / "data"
    if not data_dir.is_dir():
        raise ValueError(f"missing data directory: {data_dir}")
    artifacts = validate_finalized_artifacts(data_root, require_current_contract=True)
    parquets = artifacts["parquets"]
    if not parquets:
        raise StorageError(f"no finalized data artifacts found in {data_dir}")
    rows_total = 0
    for parquet, manifest_path in zip(parquets, artifacts["manifests"], strict=True):
        rows = validate_geoparquet(parquet)
        manifest = read_manifest(manifest_path)
        if rows != manifest.counts.included_rows:
            raise StorageError(
                f"manifest row count mismatch for {parquet.name}: "
                f"recorded {manifest.counts.included_rows}, found {rows}"
            )
        rows_total += rows
    files = len(parquets)
    print_json({"files": files, "rows": rows_total})
    return 0


def handle_card(args: SimpleNamespace) -> int:
    data_root = _data_root(args)
    stats = generate_dataset_docs(data_root, dataset_card_template())
    print_json(
        {
            "output_files": stats["output_files"],
            "rows": stats["rows"],
            "name_suffixes": stats.get("name_suffixes", {}),
        }
    )
    return 0


def handle_migrate_schema(args: SimpleNamespace) -> int:
    """Upgrade existing legacy map Parquets without reading raw PBFs."""
    data_root = _data_root(args)
    migrated = migrate_dataset_schema(data_root)
    print_json({"data_root": str(data_root), "migrated_files": migrated})
    return 0


def handle_migrate_text(args: SimpleNamespace) -> int:
    """Repair legacy untrimmed description text without reading raw PBFs."""
    data_root = _data_root(args)
    migrated = migrate_dataset_text(data_root, max_workers=args.max_workers)
    print_json({"data_root": str(data_root), "migrated_files": migrated})
    return 0


def handle_publish_plan(args: SimpleNamespace) -> int:
    data_root = _data_root(args)
    plan = create_upload_plan(data_root)
    print_json(
        {
            "repo_id": plan.repo_id,
            "identity_sha256": plan.identity_sha256,
            "files": [
                {"relative_path": item.relative_path, "sha256": item.sha256} for item in plan.files
            ],
        }
    )
    return 0


def handle_publish(args: SimpleNamespace) -> int:
    data_root = _data_root(args)
    plan = create_upload_plan(data_root)
    execute_upload(plan, confirmation=args.plan)
    print_json({"repo_id": plan.repo_id, "identity_sha256": plan.identity_sha256})
    return 0


def handle_release_stats(args: SimpleNamespace) -> int:
    """Compute, validate, and publish only the dataset card and stats report."""
    data_root = _data_root(args)
    report = release_metadata(
        data_root,
        dataset_card_template(),
        confirm_repo=args.confirm_repo,
        apply=args.apply,
    )
    print_json(report.to_payload())
    return 0


def handle_run_and_publish(args: SimpleNamespace) -> int:
    paths = _resolve_paths(args)
    tracker = TrackioRecorder(data_root=paths.data_root)
    presenter = getattr(args, "presenter", None)
    logger = (
        RunLogger(
            data_root=paths.data_root,
            run_id=str(uuid.uuid4()),
            buffer_preflight=True,
            stderr=sys.stderr,
            observer=presenter.observe,
            stderr_level=_verbosity.stderr_level,
        )
        if presenter is not None
        else None
    )
    if logger is not None:
        logger.event(
            "resolved_config",
            level="DEBUG",
            source_root=str(paths.source_root),
            data_root=str(paths.data_root),
            osmium_executable=args.osmium,
            confirm_repo=args.confirm_repo,
        )
    try:
        report = run_and_publish(
            paths=paths,
            confirm_repo=args.confirm_repo,
            osmium_executable=args.osmium,
            logger=logger,
            tracker=tracker,
        )
    finally:
        if logger is not None:
            logger.close()
    print_json(report.to_payload())
    return 0


def handle_trackio_snapshot(args: SimpleNamespace) -> int:
    data_root = _data_root(args)
    report = publish_snapshot(
        data_root,
        project=args.project,
        space_id=args.space_id,
        run_name=args.run_name,
    )
    print_json(report.to_payload())
    return 0


@app.command("inspect", help="Read-only discovery")
def inspect_command(
    source_root: SourceRoot = None,
    data_root: DataRoot = None,
    osmium: Osmium = "osmium",
) -> None:
    _invoke(
        handle_inspect,
        SimpleNamespace(source_root=source_root, data_root=data_root, osmium=osmium),
    )


@app.command("build-one", help="Build one source")
def build_one_command(
    basename: Annotated[str, typer.Argument()],
    source_root: SourceRoot = None,
    data_root: DataRoot = None,
    osmium: Osmium = "osmium",
) -> None:
    _invoke(
        handle_build_one,
        SimpleNamespace(
            source_root=source_root,
            data_root=data_root,
            osmium=osmium,
            basename=basename,
        ),
    )


@app.command(
    "build-all",
    help="Build all discovered sources",
    epilog="Example: osm-polygon-description-tag build-all "
    "--source-root /path/to/pbfs --data-root /path/to/data-root",
)
def build_all_command(
    source_root: SourceRoot = None,
    data_root: DataRoot = None,
    osmium: Osmium = "osmium",
) -> None:
    _invoke(
        handle_build_all,
        SimpleNamespace(source_root=source_root, data_root=data_root, osmium=osmium),
    )


@app.command("validate", help="Validate finalized outputs")
def validate_command(
    source_root: SourceRoot = None,
    data_root: DataRoot = None,
    osmium: Osmium = "osmium",
) -> None:
    _invoke(
        handle_validate,
        SimpleNamespace(source_root=source_root, data_root=data_root, osmium=osmium),
    )


@app.command("generate-card", help="Regenerate stats.json and README.md")
def generate_card_command(
    source_root: SourceRoot = None,
    data_root: DataRoot = None,
    osmium: Osmium = "osmium",
) -> None:
    _invoke(
        handle_card,
        SimpleNamespace(source_root=source_root, data_root=data_root, osmium=osmium),
    )


@app.command(
    "migrate-schema",
    help="Migrate legacy Parquets for Hub viewer compatibility",
)
def migrate_schema_command(
    source_root: SourceRoot = None,
    data_root: DataRoot = None,
    osmium: Osmium = "osmium",
) -> None:
    _invoke(
        handle_migrate_schema,
        SimpleNamespace(source_root=source_root, data_root=data_root, osmium=osmium),
    )


@app.command(
    "migrate-text",
    help="Repair legacy untrimmed description text in published Parquets",
)
def migrate_text_command(
    max_workers: Annotated[
        int,
        typer.Option("--max-workers", help="Artifacts to repair concurrently"),
    ] = 1,
    source_root: SourceRoot = None,
    data_root: DataRoot = None,
    osmium: Osmium = "osmium",
) -> None:
    _invoke(
        handle_migrate_text,
        SimpleNamespace(
            source_root=source_root,
            data_root=data_root,
            osmium=osmium,
            max_workers=max_workers,
        ),
    )


@app.command("trackio-snapshot", help="Log a completed dataset snapshot to Trackio")
def trackio_snapshot_command(
    project: Annotated[
        str, typer.Option("--project", help="Trackio project name.")
    ] = "osm-polygon-description-tag",
    space_id: Annotated[
        str, typer.Option("--space-id", help="Hugging Face Space hosting the dashboard.")
    ] = "NoeFlandre/osm-polygon-description-tag-trackio",
    run_name: Annotated[
        str | None, typer.Option("--run-name", help="Trackio run name [default: generated].")
    ] = None,
    source_root: SourceRoot = None,
    data_root: DataRoot = None,
    osmium: Osmium = "osmium",
) -> None:
    _invoke(
        handle_trackio_snapshot,
        SimpleNamespace(
            source_root=source_root,
            data_root=data_root,
            osmium=osmium,
            project=project,
            space_id=space_id,
            run_name=run_name,
        ),
    )


@app.command(
    "publish-plan",
    help="Show the allowlisted upload plan identity",
    epilog="Example: osm-polygon-description-tag publish-plan --data-root /path/to/data-root",
)
def publish_plan_command(
    source_root: SourceRoot = None,
    data_root: DataRoot = None,
    osmium: Osmium = "osmium",
) -> None:
    _invoke(
        handle_publish_plan,
        SimpleNamespace(source_root=source_root, data_root=data_root, osmium=osmium),
    )


@app.command(
    "publish",
    help="Upload after exact plan confirmation",
    epilog="Example: osm-polygon-description-tag publish --data-root /path/to/data-root "
    "--plan <identity_sha256 from publish-plan>",
)
def publish_command(
    plan: Annotated[
        str,
        typer.Option(
            "--plan",
            help="Plan identity SHA-256 (must match freshly computed identity)",
        ),
    ],
    source_root: SourceRoot = None,
    data_root: DataRoot = None,
    osmium: Osmium = "osmium",
) -> None:
    _invoke(
        handle_publish,
        SimpleNamespace(
            source_root=source_root,
            data_root=data_root,
            osmium=osmium,
            plan=plan,
        ),
    )


@app.command(
    "release-stats",
    help="Publish only the card and stats report",
)
def release_stats_command(
    confirm_repo: Annotated[
        str,
        typer.Option(
            "--confirm-repo",
            help="Exact dataset repo id (must equal NoeFlandre/osm-polygon-description-tag)",
        ),
    ],
    apply: Annotated[
        bool,
        typer.Option("--apply/--dry-run", help="Upload and verify (default: dry run)"),
    ] = False,
    source_root: SourceRoot = None,
    data_root: DataRoot = None,
    osmium: Osmium = "osmium",
) -> None:
    _invoke(
        handle_release_stats,
        SimpleNamespace(
            source_root=source_root,
            data_root=data_root,
            osmium=osmium,
            confirm_repo=confirm_repo,
            apply=apply,
        ),
    )


@app.command(
    "run-and-publish",
    help="Stoppable, resumable build+publish for every discovered PBF",
    epilog="Example: osm-polygon-description-tag -q run-and-publish "
    "--confirm-repo NoeFlandre/osm-polygon-description-tag "
    "--source-root /path/to/pbfs --data-root /path/to/data-root",
)
def run_and_publish_command(
    confirm_repo: Annotated[
        str,
        typer.Option(
            "--confirm-repo",
            help="Exact dataset repo id (must equal NoeFlandre/osm-polygon-description-tag)",
        ),
    ],
    source_root: SourceRoot = None,
    data_root: DataRoot = None,
    osmium: Osmium = "osmium",
) -> None:
    presenter = TerminalPresenter(stderr=sys.stderr)
    try:
        _invoke(
            handle_run_and_publish,
            SimpleNamespace(
                source_root=source_root,
                data_root=data_root,
                osmium=osmium,
                confirm_repo=confirm_repo,
                presenter=presenter,
            ),
        )
    finally:
        presenter.close()


_ERROR_TYPES = (
    OSError,
    ValueError,
    OsmiumExportError,
    ManifestError,
    StorageError,
    ReportingError,
    PublicationError,
    HubVerificationError,
    PreflightError,
    OrchestratorError,
    MigrationError,
    TextMigrationError,
    LanguageDetectionError,
    DriverError,
)

# Documented in docs/cli.md. Usage errors exit 2 and Ctrl-C exits 130.
EXIT_GENERIC = 1
EXIT_ENVIRONMENT = 3
EXIT_VALIDATION = 4
EXIT_PUBLICATION = 5
_EXIT_CODES: tuple[tuple[tuple[type[Exception], ...], int], ...] = (
    ((PreflightError, MissingPathError, UnsafePathError), EXIT_ENVIRONMENT),
    ((ManifestError, StorageError, ReportingError), EXIT_VALIDATION),
    ((PublicationError, HubVerificationError), EXIT_PUBLICATION),
)


def exit_code_for(error: Exception) -> int:
    """Map a domain error to its documented exit code."""
    return next((code for types, code in _EXIT_CODES if isinstance(error, types)), EXIT_GENERIC)


def _show_click_error(error: ClickException) -> None:
    if isinstance(error, UsageError) and error.ctx is not None:
        usage = error.ctx.get_usage()
        if usage.startswith("Usage:"):
            usage = "usage:" + usage.removeprefix("Usage:")
        print(usage, file=sys.stderr)  # noqa: T201 - usage errors go to stderr
        print(f"error: {error.format_message()}", file=sys.stderr)  # noqa: T201 - as above
        return
    error.show(file=sys.stderr)


def run(argv: Sequence[str] | None = None) -> int:
    _configure_terminal()
    try:
        return _invoke_app(argv)
    except Exit as error:
        return int(error.exit_code)
    except ClickException as error:
        _show_click_error(error)
        return int(error.exit_code)
    except (KeyboardInterrupt, _Interrupted):
        return 130
    except _ERROR_TYPES as error:
        # pragma: no mutate start - None uses the current stderr by default
        presenter = TerminalPresenter(stderr=sys.stderr)
        # pragma: no mutate end
        presenter.error(str(error))
        return exit_code_for(error)


def _configure_terminal() -> None:
    # Rich may cache a zero-width value before the entry point runs (notably in
    # CI/pre-commit subprocesses). Keep captured help deterministic and readable.
    rich_utils.MAX_WIDTH = 80
    if not sys.stdout.isatty():
        rich_utils.FORCE_TERMINAL = False
    _normalize_columns()


def _normalize_columns() -> None:
    columns = os.environ.get("COLUMNS")
    if columns is None:
        return
    try:
        invalid_columns = int(columns) < 80
    except ValueError:
        invalid_columns = True
    if invalid_columns:
        os.environ["COLUMNS"] = "80"


def _invoke_app(argv: Sequence[str] | None) -> int:
    try:
        # Without standalone mode, Click returns an Exit's code instead of raising.
        code = app(
            args=list(argv) if argv is not None else None,
            prog_name="osm-polygon-description-tag",
            standalone_mode=False,
        )
    finally:
        # -v / -q apply to one invocation only.
        _verbosity.stderr_level = "INFO"
    return code if isinstance(code, int) else 0


def main() -> None:
    raise SystemExit(run())


if __name__ == "__main__":
    main()


__all__ = [
    "app",
    "handle_build_all",
    "handle_build_one",
    "handle_card",
    "handle_inspect",
    "handle_publish",
    "handle_publish_plan",
    "handle_run_and_publish",
    "handle_validate",
    "main",
    "run",
]
