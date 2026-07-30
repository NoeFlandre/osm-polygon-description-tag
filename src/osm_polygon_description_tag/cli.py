"""Typer CLI for deterministic polygon dataset build and publication.

Successful commands write one JSON document to stdout. Usage diagnostics,
operational events, and domain errors are confined to stderr.
"""

from __future__ import annotations

import json
import sys
import uuid
from collections.abc import Callable, Sequence
from pathlib import Path
from types import SimpleNamespace
from typing import Annotated, Any

import typer
from typer._click.exceptions import ClickException, Exit, UsageError

from osm_polygon_description_tag.dataset.manifest import ManifestError
from osm_polygon_description_tag.dataset.reporting import ReportingError, generate_dataset_docs
from osm_polygon_description_tag.dataset.storage import StorageError, validate_geoparquet
from osm_polygon_description_tag.osm.discovery import discover_sources
from osm_polygon_description_tag.osm.extraction import OsmiumExportError
from osm_polygon_description_tag.publication import (
    PublicationError,
    create_upload_plan,
    execute_upload,
)
from osm_polygon_description_tag.runtime.config import Paths
from osm_polygon_description_tag.runtime.logging import RunLogger
from osm_polygon_description_tag.runtime.presentation import TerminalPresenter
from osm_polygon_description_tag.runtime.resources import (
    dataset_card_template,
    osmium_export_config,
)
from osm_polygon_description_tag.workflow.build import BuildResult, build_all, build_one
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

SourceRoot = Annotated[Path | None, typer.Option("--source-root")]
DataRoot = Annotated[Path | None, typer.Option("--data-root")]
Osmium = Annotated[str, typer.Option("--osmium")]


def _namespace(
    *,
    source_root: Path | None,
    data_root: Path | None,
    osmium: str,
    **values: object,
) -> SimpleNamespace:
    return SimpleNamespace(
        source_root=source_root,
        data_root=data_root,
        osmium=osmium,
        **values,
    )


def _resolve_paths(args: SimpleNamespace) -> Paths:
    defaults = Paths.defaults()
    return Paths(
        source_root=args.source_root or defaults.source_root,
        data_root=args.data_root or defaults.data_root,
    ).validate()


def _print_json(payload: dict[str, object]) -> None:
    print(json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True))


class _Interrupted(Exception):
    """Carry Ctrl-C through Typer without its default exit-code conversion."""


def _invoke(handler: Callable[[SimpleNamespace], int], args: SimpleNamespace) -> None:
    try:
        handler(args)
    except KeyboardInterrupt as error:
        raise _Interrupted from error


def handle_inspect(args: SimpleNamespace) -> int:
    paths = _resolve_paths(args)
    sources = discover_sources(paths.source_root)
    _print_json(
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
    _print_json(
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
    _print_json(
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
    paths = _resolve_paths(args)
    data_dir = paths.data_root / "data"
    if not data_dir.is_dir():
        raise ValueError(f"missing data directory: {data_dir}")
    rows_total = 0
    files = 0
    for parquet in sorted(data_dir.glob("*.parquet"), key=lambda path: path.name):
        rows_total += validate_geoparquet(parquet)
        files += 1
    _print_json({"files": files, "rows": rows_total})
    return 0


def handle_card(args: SimpleNamespace) -> int:
    paths = _resolve_paths(args)
    stats = generate_dataset_docs(paths.data_root, dataset_card_template())
    _print_json(
        {
            "output_files": stats["output_files"],
            "rows": stats["rows"],
            "name_suffixes": stats.get("name_suffixes", {}),
        }
    )
    return 0


def handle_publish_plan(args: SimpleNamespace) -> int:
    paths = _resolve_paths(args)
    plan = create_upload_plan(paths.data_root)
    _print_json(
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
    paths = _resolve_paths(args)
    plan = create_upload_plan(paths.data_root)
    execute_upload(plan, confirmation=args.plan)
    _print_json({"repo_id": plan.repo_id, "identity_sha256": plan.identity_sha256})
    return 0


def handle_run_and_publish(args: SimpleNamespace) -> int:
    paths = _resolve_paths(args)
    presenter = getattr(args, "presenter", None)
    logger = (
        RunLogger(
            data_root=paths.data_root,
            run_id=str(uuid.uuid4()),
            buffer_preflight=True,
            stderr=sys.stderr,
            observer=presenter.observe,
        )
        if presenter is not None
        else None
    )
    try:
        report = run_and_publish(
            paths=paths,
            confirm_repo=args.confirm_repo,
            osmium_executable=args.osmium,
            logger=logger,
        )
    finally:
        if logger is not None:
            logger.close()
    _print_json(report.to_payload())
    return 0


@app.command("inspect", help="Read-only discovery")
def inspect_command(
    source_root: SourceRoot = None,
    data_root: DataRoot = None,
    osmium: Osmium = "osmium",
) -> None:
    _invoke(
        handle_inspect,
        _namespace(source_root=source_root, data_root=data_root, osmium=osmium),
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
        _namespace(
            source_root=source_root,
            data_root=data_root,
            osmium=osmium,
            basename=basename,
        ),
    )


@app.command("build-all", help="Build all discovered sources")
def build_all_command(
    source_root: SourceRoot = None,
    data_root: DataRoot = None,
    osmium: Osmium = "osmium",
) -> None:
    _invoke(
        handle_build_all,
        _namespace(source_root=source_root, data_root=data_root, osmium=osmium),
    )


@app.command("validate", help="Validate finalized outputs")
def validate_command(
    source_root: SourceRoot = None,
    data_root: DataRoot = None,
    osmium: Osmium = "osmium",
) -> None:
    _invoke(
        handle_validate,
        _namespace(source_root=source_root, data_root=data_root, osmium=osmium),
    )


@app.command("generate-card", help="Regenerate stats.json and README.md")
def generate_card_command(
    source_root: SourceRoot = None,
    data_root: DataRoot = None,
    osmium: Osmium = "osmium",
) -> None:
    _invoke(
        handle_card,
        _namespace(source_root=source_root, data_root=data_root, osmium=osmium),
    )


@app.command("publish-plan", help="Show the allowlisted upload plan identity")
def publish_plan_command(
    source_root: SourceRoot = None,
    data_root: DataRoot = None,
    osmium: Osmium = "osmium",
) -> None:
    _invoke(
        handle_publish_plan,
        _namespace(source_root=source_root, data_root=data_root, osmium=osmium),
    )


@app.command("publish", help="Upload after exact plan confirmation")
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
        _namespace(
            source_root=source_root,
            data_root=data_root,
            osmium=osmium,
            plan=plan,
        ),
    )


@app.command(
    "run-and-publish",
    help="Stoppable, resumable build+publish for every discovered PBF",
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
            _namespace(
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
    PreflightError,
    OrchestratorError,
)


def _show_click_error(error: ClickException) -> None:
    if isinstance(error, UsageError) and error.ctx is not None:
        usage = error.ctx.get_usage()
        if usage.startswith("Usage:"):
            usage = "usage:" + usage.removeprefix("Usage:")
        print(usage, file=sys.stderr)
        print(f"error: {error.format_message()}", file=sys.stderr)
        return
    error.show(file=sys.stderr)


def run(argv: Sequence[str] | None = None) -> int:
    try:
        app(
            args=list(argv) if argv is not None else None,
            prog_name="osm-polygon-description-tag",
            standalone_mode=False,
        )
        return 0
    except Exit as error:
        return int(error.exit_code)
    except ClickException as error:
        _show_click_error(error)
        return int(error.exit_code)
    except (KeyboardInterrupt, _Interrupted):
        return 130
    except _ERROR_TYPES as error:
        TerminalPresenter(stderr=sys.stderr).error(str(error))
        return 1


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
