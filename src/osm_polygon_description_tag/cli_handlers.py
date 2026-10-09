"""Command handlers for the console entry point.

Each handler runs one operation from the frozen request that a Typer
declaration in ``cli`` built, then prints the JSON result. Domain errors
propagate to ``cli.run``, which maps them to exit codes and stderr messages.
"""

from __future__ import annotations

import os
import sys
import uuid
from collections.abc import Callable
from pathlib import Path
from typing import Protocol

from osm_polygon_description_tag.cli_requests import (
    BuildOneRequest,
    MigrateTextRequest,
    PathOptions,
    PublishRequest,
    ReleaseStatsRequest,
    RunAndPublishRequest,
    TrackioSnapshotRequest,
)
from osm_polygon_description_tag.dataset.docs import generate_dataset_docs
from osm_polygon_description_tag.dataset.migration import migrate_dataset_schema
from osm_polygon_description_tag.dataset.text_migration import migrate_dataset_text
from osm_polygon_description_tag.dataset.validation import validate_dataset_outputs
from osm_polygon_description_tag.observability.trackio import (
    TrackioRecorder,
    publish_snapshot,
)
from osm_polygon_description_tag.osm.discovery import Source, discover_sources
from osm_polygon_description_tag.publication import (
    create_upload_plan,
    execute_upload,
    release_metadata,
)
from osm_polygon_description_tag.runtime.config import (
    SOURCE_ROOT_ENV,
    MissingPathError,
    Paths,
    resolve_data_root,
)
from osm_polygon_description_tag.runtime.logging import RunLogger
from osm_polygon_description_tag.runtime.presentation import print_json
from osm_polygon_description_tag.runtime.resources import (
    dataset_card_template,
    osmium_export_config,
)
from osm_polygon_description_tag.workflow.build import BuildResult, build_all, build_one
from osm_polygon_description_tag.workflow.orchestrator import run_and_publish


class _DataRootOption(Protocol):
    @property
    def data_root(self) -> Path | None: ...


class _RootOptions(_DataRootOption, Protocol):
    @property
    def source_root(self) -> Path | None: ...


def _resolve_paths(args: _RootOptions) -> Paths:
    return Paths.resolve(args.source_root, args.data_root)


def _data_root(args: _DataRootOption) -> Path:
    """Data-only commands need no source root."""
    return resolve_data_root(args.data_root)


def handle_inspect(args: PathOptions) -> None:
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


def _build_paths_and_executor(
    args: PathOptions,
) -> tuple[Paths, Callable[[Source], BuildResult]]:
    paths = _resolve_paths(args)

    def executor(source: Source) -> BuildResult:
        return build_one(
            source,
            paths,
            export_config=osmium_export_config(),
            executable=args.osmium,
        )

    return paths, executor


def handle_build_one(args: BuildOneRequest) -> None:
    paths, executor = _build_paths_and_executor(args)
    sources = discover_sources(paths.source_root)
    match = next((source for source in sources if source.name == args.basename), None)
    if match is None:
        raise MissingPathError(f"source not discovered: {args.basename}")
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


def handle_build_all(args: PathOptions) -> None:
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


def handle_validate(args: PathOptions) -> None:
    data_root = _data_root(args)
    source_root = _validation_source_root(args, data_root)
    summary = validate_dataset_outputs(data_root, source_root)
    print_json({"files": summary.files, "rows": summary.rows})


def _validation_source_root(args: PathOptions, data_root: Path) -> Path | None:
    """Return the source root to check, or None when neither the option nor the env sets one."""
    source_root = args.source_root
    if source_root is None and not os.environ.get(SOURCE_ROOT_ENV, "").strip():
        return None
    return Paths.resolve(source_root, data_root).source_root


def handle_card(args: PathOptions) -> None:
    data_root = _data_root(args)
    stats = generate_dataset_docs(data_root, dataset_card_template())
    print_json(
        {
            "output_files": stats["output_files"],
            "rows": stats["rows"],
            "name_suffixes": stats.get("name_suffixes", {}),
        }
    )


def handle_migrate_schema(args: PathOptions) -> None:
    """Upgrade existing legacy map Parquets without reading raw PBFs."""
    data_root = _data_root(args)
    migrated = migrate_dataset_schema(data_root)
    print_json({"data_root": str(data_root), "migrated_files": migrated})


def handle_migrate_text(args: MigrateTextRequest) -> None:
    """Repair legacy untrimmed description text without reading raw PBFs."""
    data_root = _data_root(args)
    migrated = migrate_dataset_text(data_root, max_workers=args.max_workers)
    print_json({"data_root": str(data_root), "migrated_files": migrated})


def handle_publish_plan(args: PathOptions) -> None:
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


def handle_publish(args: PublishRequest) -> None:
    data_root = _data_root(args)
    plan = create_upload_plan(data_root)
    execute_upload(plan, confirmation=args.plan)
    print_json({"repo_id": plan.repo_id, "identity_sha256": plan.identity_sha256})


def handle_release_stats(args: ReleaseStatsRequest) -> None:
    """Compute, validate, and publish only the dataset card and stats report."""
    data_root = _data_root(args)
    report = release_metadata(
        data_root,
        dataset_card_template(),
        confirm_repo=args.confirm_repo,
        apply=args.apply,
    )
    print_json(report.to_payload())


def handle_run_and_publish(args: RunAndPublishRequest) -> None:
    paths = _resolve_paths(args)
    tracker = TrackioRecorder(data_root=paths.data_root)
    presenter = args.presenter
    logger = (
        RunLogger(
            data_root=paths.data_root,
            run_id=str(uuid.uuid4()),
            buffer_preflight=True,
            stderr=sys.stderr,
            observer=presenter.observe,
            stderr_level=args.stderr_level,
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


def handle_trackio_snapshot(args: TrackioSnapshotRequest) -> None:
    data_root = _data_root(args)
    report = publish_snapshot(
        data_root,
        project=args.project,
        space_id=args.space_id,
        run_name=args.run_name,
    )
    print_json(report.to_payload())


__all__ = [
    "handle_build_all",
    "handle_build_one",
    "handle_card",
    "handle_inspect",
    "handle_migrate_schema",
    "handle_migrate_text",
    "handle_publish",
    "handle_publish_plan",
    "handle_release_stats",
    "handle_run_and_publish",
    "handle_trackio_snapshot",
    "handle_validate",
]
