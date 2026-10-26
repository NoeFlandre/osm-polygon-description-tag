"""Typer CLI for deterministic polygon dataset build and publication.

Successful commands write one JSON document to stdout. Usage diagnostics,
operational events, and domain errors are confined to stderr.
"""

from __future__ import annotations

import os
import sys
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from importlib.metadata import version as package_version
from pathlib import Path
from typing import Annotated

import typer
from typer import rich_utils

from osm_polygon_description_tag.cli_handlers import (
    handle_build_all,
    handle_build_one,
    handle_card,
    handle_inspect,
    handle_migrate_schema,
    handle_migrate_text,
    handle_publish,
    handle_publish_plan,
    handle_release_stats,
    handle_run_and_publish,
    handle_trackio_snapshot,
    handle_validate,
)
from osm_polygon_description_tag.cli_requests import (
    DEFAULT_STDERR_LEVEL,
    BuildOneRequest,
    MigrateTextRequest,
    PathOptions,
    PublishRequest,
    ReleaseStatsRequest,
    RunAndPublishRequest,
    TrackioSnapshotRequest,
)
from osm_polygon_description_tag.dataset.languages.detector import LanguageDetectionError
from osm_polygon_description_tag.dataset.manifest import ManifestError
from osm_polygon_description_tag.dataset.migration import MigrationError
from osm_polygon_description_tag.dataset.stats_manifest import ReportingError
from osm_polygon_description_tag.dataset.storage_errors import StorageError
from osm_polygon_description_tag.dataset.text_migration import TextMigrationError
from osm_polygon_description_tag.language_cli import language_app
from osm_polygon_description_tag.osm.extraction import OsmiumExportError
from osm_polygon_description_tag.publication import PublicationError
from osm_polygon_description_tag.publication.verification import HubVerificationError
from osm_polygon_description_tag.runtime.click_compat import ClickException, Exit, UsageError
from osm_polygon_description_tag.runtime.config import MissingPathError, UnsafePathError
from osm_polygon_description_tag.runtime.presentation import TerminalPresenter
from osm_polygon_description_tag.workflow.grid_driver import DriverError
from osm_polygon_description_tag.workflow.orchestrator import OrchestratorError
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
        "Validation also checks source bytes when this is provided.",
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


@dataclass
class _GlobalOptions:
    """Root-callback options for one invocation, stored on that invocation's Click context."""

    # stderr threshold for human-readable event lines, set by -v / -q. The JSONL
    # log always records every event.
    stderr_level: str = DEFAULT_STDERR_LEVEL


def _show_version(value: bool) -> None:
    if value:
        typer.echo(package_version(_DISTRIBUTION))
        raise Exit


@app.callback()
def _global_options(
    ctx: typer.Context,
    _version: Annotated[
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
    level = "DEBUG" if verbose else "WARNING" if quiet else DEFAULT_STDERR_LEVEL
    ctx.obj = _GlobalOptions(stderr_level=level)


class _Interrupted(Exception):  # noqa: N818 - a control-flow signal, not an error
    """Carry Ctrl-C through Typer without its default exit-code conversion."""


def _invoke[RequestT](handler: Callable[[RequestT], None], args: RequestT) -> None:
    try:
        handler(args)
    except KeyboardInterrupt as error:
        raise _Interrupted from error


@app.command("inspect", help="Read-only discovery")
def inspect_command(
    source_root: SourceRoot = None,
    data_root: DataRoot = None,
    osmium: Osmium = "osmium",
) -> None:
    _invoke(
        handle_inspect,
        PathOptions(source_root=source_root, data_root=data_root, osmium=osmium),
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
        BuildOneRequest(
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
        PathOptions(source_root=source_root, data_root=data_root, osmium=osmium),
    )


@app.command("validate", help="Validate finalized outputs")
def validate_command(
    source_root: SourceRoot = None,
    data_root: DataRoot = None,
    osmium: Osmium = "osmium",
) -> None:
    _invoke(
        handle_validate,
        PathOptions(source_root=source_root, data_root=data_root, osmium=osmium),
    )


@app.command("generate-card", help="Regenerate stats.json and README.md")
def generate_card_command(
    source_root: SourceRoot = None,
    data_root: DataRoot = None,
    osmium: Osmium = "osmium",
) -> None:
    _invoke(
        handle_card,
        PathOptions(source_root=source_root, data_root=data_root, osmium=osmium),
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
        PathOptions(source_root=source_root, data_root=data_root, osmium=osmium),
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
        MigrateTextRequest(
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
        TrackioSnapshotRequest(
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
        PathOptions(source_root=source_root, data_root=data_root, osmium=osmium),
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
        PublishRequest(
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
        ReleaseStatsRequest(
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
    ctx: typer.Context,
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
            RunAndPublishRequest(
                source_root=source_root,
                data_root=data_root,
                osmium=osmium,
                confirm_repo=confirm_repo,
                presenter=presenter,
                stderr_level=ctx.ensure_object(_GlobalOptions).stderr_level,
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
        typer.echo(usage, err=True)
        typer.echo(f"error: {error.format_message()}", err=True)
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
    # Without standalone mode, Click returns an Exit's code instead of raising.
    # -v / -q live on this invocation's Click context, so they end with it.
    code = app(
        args=list(argv) if argv is not None else None,
        prog_name="osm-polygon-description-tag",
        standalone_mode=False,
    )
    return code if isinstance(code, int) else 0


def main() -> None:
    raise SystemExit(run())


if __name__ == "__main__":
    main()


__all__ = [
    "app",
    "main",
    "run",
]
