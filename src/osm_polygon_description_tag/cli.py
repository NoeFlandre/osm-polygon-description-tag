"""Public CLI mapping arguments to the pipeline's public module functions."""

import argparse
import json
import sys
from collections.abc import Callable, Sequence
from pathlib import Path
from typing import Any

from osm_polygon_description_tag.config import Paths
from osm_polygon_description_tag.discovery import discover_sources
from osm_polygon_description_tag.extraction import OsmiumExportError
from osm_polygon_description_tag.manifest import ManifestError
from osm_polygon_description_tag.pipeline import BuildResult, build_all, build_one
from osm_polygon_description_tag.publication import (
    PublicationError,
    create_upload_plan,
    execute_upload,
)
from osm_polygon_description_tag.reporting import ReportingError, generate_dataset_docs
from osm_polygon_description_tag.storage import StorageError, validate_geoparquet

_DEFAULT_EXPORT_CONFIG = Path("config/osmium-export.json")
_DEFAULT_CARD_TEMPLATE = Path("docs/dataset-card-template.md")


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
            "export_config": str(args.export_config)
            if getattr(args, "export_config", None)
            else None,
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
) -> tuple[Paths, "Callable[[Any], BuildResult]"]:
    paths = _resolve_paths(args)

    def executor(source: Any) -> BuildResult:
        return build_one(
            source,
            paths,
            export_config=args.export_config,
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
    stats = generate_dataset_docs(paths.data_root, args.template)
    _print_json(
        {
            "output_files": stats["output_files"],
            "rows": stats["rows"],
            "generation_timestamp_utc": stats["generation_timestamp_utc"],
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
    execute_upload(plan, confirmation=args.confirm)
    _print_json({"repo_id": plan.repo_id, "identity_sha256": plan.identity_sha256})
    return 0


def create_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="osm-polygon-description-tag")
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("--source-root", type=Path, default=None)
    common.add_argument("--data-root", type=Path, default=None)
    common.add_argument("--osmium", default="osmium")
    common.add_argument("--export-config", type=Path, default=_DEFAULT_EXPORT_CONFIG)

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
    sub.add_argument("--template", type=Path, default=_DEFAULT_CARD_TEMPLATE)
    sub.set_defaults(handler=handle_card)

    sub = subparsers.add_parser(
        "publish-plan", parents=[common], help="Show the allowlisted upload plan identity"
    )
    sub.set_defaults(handler=handle_publish_plan)

    sub = subparsers.add_parser(
        "publish", parents=[common], help="Upload after exact plan confirmation"
    )
    sub.add_argument("--plan", required=True, help="Plan identity SHA-256 (required)")
    sub.add_argument("--confirm", required=True, help="Confirmation SHA-256 (must equal plan)")
    sub.set_defaults(handler=handle_publish)

    return parser


_ERROR_TYPES = (
    OSError,
    ValueError,
    OsmiumExportError,
    ManifestError,
    StorageError,
    ReportingError,
    PublicationError,
)


def run(argv: Sequence[str] | None = None) -> int:
    parser = create_parser()
    args = parser.parse_args(argv)
    try:
        return int(args.handler(args))
    except _ERROR_TYPES as error:
        print(f"error: {error}", file=sys.stderr)
        return 1
