"""Public CLI mapping arguments to the pipeline's public module functions.

Subcommands:

- ``inspect``: read-only source discovery and preflight.
- ``build-one``: build one named PBF output.
- ``build-all``: deterministic, resumable orchestration.
- ``validate``: validate selected or all finalized outputs.
- ``generate-card``: regenerate ``stats.json`` and the dataset card.
- ``publish-plan``: validate and show the exact prospective upload.
- ``publish``: separately confirmed execution of an unchanged plan.
- ``run-and-publish``: stoppable, resumable build + publish for every PBF.

All file-path defaults are resolved against the installed package, not the
caller's working directory.
"""

from __future__ import annotations

import argparse
import json
import sys
from collections.abc import Callable, Sequence
from pathlib import Path
from typing import Any

from osm_polygon_description_tag.dataset.manifest import ManifestError
from osm_polygon_description_tag.dataset.reporting import ReportingError, generate_dataset_docs
from osm_polygon_description_tag.dataset.storage import StorageError, validate_geoparquet
from osm_polygon_description_tag.orchestrator import (
    OrchestratorError,
    PreflightError,
    run_and_publish,
)
from osm_polygon_description_tag.osm.discovery import discover_sources
from osm_polygon_description_tag.osm.extraction import OsmiumExportError
from osm_polygon_description_tag.pipeline import BuildResult, build_all, build_one
from osm_polygon_description_tag.publication import (
    PublicationError,
    create_upload_plan,
    execute_upload,
)
from osm_polygon_description_tag.runtime.config import Paths
from osm_polygon_description_tag.runtime.resources import (
    dataset_card_template,
    osmium_export_config,
)


def _resolve_paths(args: argparse.Namespace) -> Paths:
    if args.source_root is None and args.data_root is None:
        paths = Paths.defaults()
    else:
        defaults = Paths.defaults()
        paths = Paths(
            source_root=args.source_root or defaults.source_root,
            data_root=args.data_root or defaults.data_root,
        )
    return paths.validate()


def _print_json(payload: dict[str, object]) -> None:
    print(json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True))


def handle_inspect(args: argparse.Namespace) -> int:
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
    args: argparse.Namespace,
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


def handle_build_one(args: argparse.Namespace) -> int:
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


def handle_build_all(args: argparse.Namespace) -> int:
    paths, executor = _build_paths_and_executor(args)
    sources = discover_sources(paths.source_root)
    results: list[BuildResult] = build_all(sources, build=executor)
    _print_json(
        {
            "count": len(results),
            "results": [
                {
                    "source_name": r.source_name,
                    "status": r.status,
                    "included_rows": r.included_rows,
                }
                for r in results
            ],
        }
    )
    return 0


def handle_validate(args: argparse.Namespace) -> int:
    paths = _resolve_paths(args)
    data_dir = paths.data_root / "data"
    if not data_dir.is_dir():
        raise ValueError(f"missing data directory: {data_dir}")
    rows_total = 0
    files = 0
    for parquet in sorted(data_dir.glob("*.parquet"), key=lambda p: p.name):
        rows_total += validate_geoparquet(parquet)
        files += 1
    _print_json({"files": files, "rows": rows_total})
    return 0


def handle_card(args: argparse.Namespace) -> int:
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


def handle_publish_plan(args: argparse.Namespace) -> int:
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


def handle_publish(args: argparse.Namespace) -> int:
    paths = _resolve_paths(args)
    plan = create_upload_plan(paths.data_root)
    execute_upload(plan, confirmation=args.plan)
    _print_json({"repo_id": plan.repo_id, "identity_sha256": plan.identity_sha256})
    return 0


def handle_run_and_publish(args: argparse.Namespace) -> int:
    paths = _resolve_paths(args)
    report = run_and_publish(
        paths=paths,
        confirm_repo=args.confirm_repo,
        osmium_executable=args.osmium,
    )
    _print_json(report.to_payload())
    return 0


def create_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="osm-polygon-description-tag")
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("--source-root", type=Path, default=None)
    common.add_argument("--data-root", type=Path, default=None)
    common.add_argument("--osmium", default="osmium")

    subparsers = parser.add_subparsers(dest="command", required=True)

    sub = subparsers.add_parser("inspect", parents=[common], help="Read-only discovery")
    sub.set_defaults(handler=handle_inspect)

    sub = subparsers.add_parser("build-one", parents=[common], help="Build one source")
    sub.add_argument("basename")
    sub.set_defaults(handler=handle_build_one)

    sub = subparsers.add_parser("build-all", parents=[common], help="Build all discovered sources")
    sub.set_defaults(handler=handle_build_all)

    sub = subparsers.add_parser("validate", parents=[common], help="Validate finalized outputs")
    sub.set_defaults(handler=handle_validate)

    sub = subparsers.add_parser(
        "generate-card", parents=[common], help="Regenerate stats.json and README.md"
    )
    sub.set_defaults(handler=handle_card)

    sub = subparsers.add_parser(
        "publish-plan", parents=[common], help="Show the allowlisted upload plan identity"
    )
    sub.set_defaults(handler=handle_publish_plan)

    sub = subparsers.add_parser(
        "publish", parents=[common], help="Upload after exact plan confirmation"
    )
    sub.add_argument(
        "--plan", required=True, help="Plan identity SHA-256 (must match freshly computed identity)"
    )
    sub.set_defaults(handler=handle_publish)

    sub = subparsers.add_parser(
        "run-and-publish",
        parents=[common],
        help="Stoppable, resumable build+publish for every discovered PBF",
    )
    sub.add_argument(
        "--confirm-repo",
        required=True,
        help="Exact dataset repo id (must equal NoeFlandre/osm-polygon-description-tag)",
    )
    sub.set_defaults(handler=handle_run_and_publish)

    return parser


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


def run(argv: Sequence[str] | None = None) -> int:
    parser = create_parser()
    args = parser.parse_args(argv)
    try:
        return int(args.handler(args))
    except KeyboardInterrupt:
        return 130
    except _ERROR_TYPES as error:
        print(f"error: {error}", file=sys.stderr)
        return 1


__all__ = [
    "create_parser",
    "handle_inspect",
    "handle_publish",
    "handle_publish_plan",
    "handle_run_and_publish",
    "handle_validate",
    "run",
]
