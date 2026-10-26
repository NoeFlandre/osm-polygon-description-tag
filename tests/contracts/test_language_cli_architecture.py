"""Contracts that keep Typer wiring separate from language workflows."""

import ast
import os
from pathlib import Path

import pytest

SOURCE_ROOT = Path(__file__).parents[2] / "src" / "osm_polygon_description_tag"
CLI_PATH = SOURCE_ROOT / "language_cli.py"
WORKFLOW_MODULES = (
    SOURCE_ROOT / "workflow" / "language_workflow.py",
    SOURCE_ROOT / "workflow" / "grid_transport.py",
    SOURCE_ROOT / "workflow" / "grid_workflow.py",
    SOURCE_ROOT / "workflow" / "publication_workflow.py",
)

MOVED_WORKFLOWS = {
    "_build_detector_for_snapshot",
    "capture_policy",
    "_copy_retrieval_snapshot",
    "_emit_local_collection",
    "_emit_retrieval_plan",
    "execute_transport",
    "_existing_job_directory",
    "_infer_remote_bundle_dir",
    "_import_and_acknowledge_collection",
    "_is_portable_payload",
    "resolve_language_policy",
    "_portable_payload_candidates",
    "portable_remote_paths",
    "_prepare_identity",
    "_prepare_report",
    "_prepare_retrieval_directory",
    "_raise_transport_failure",
    "remote_child",
    "_require_retrieved_run_dir",
    "_retrieve_and_collect",
    "_reuse_verified_staged_job",
    "_retrieval_snapshot_source",
    "_retrieval_snapshot_target",
    "seed_retrieval_snapshot",
    "transport_payload",
    "_verify_local_staged_files",
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
}


def _defined_functions(path: Path) -> set[str]:
    tree = ast.parse(path.read_text(encoding="utf-8"))
    return {
        node.name
        for node in ast.walk(tree)
        if isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef)
    }


def test_language_cli_is_only_command_wiring() -> None:
    if "MUTANT_UNDER_TEST" in os.environ:
        pytest.skip("static architecture bounds are checked on the canonical source tree")
    assert CLI_PATH.read_text(encoding="utf-8").count("\n") < 600
    assert not (_defined_functions(CLI_PATH) & MOVED_WORKFLOWS)


def test_clock_helper_is_defined_only_in_runtime_time() -> None:
    defining = {
        path.relative_to(SOURCE_ROOT)
        for path in SOURCE_ROOT.rglob("*.py")
        if "utc_now" in _defined_functions(path)
    }
    assert defining == {Path("runtime") / "time.py"}


def test_language_workflow_modules_exist_and_are_bounded() -> None:
    if "MUTANT_UNDER_TEST" in os.environ:
        pytest.skip("static architecture bounds are checked on the canonical source tree")
    assert all(path.is_file() for path in WORKFLOW_MODULES)
    assert all(path.read_text(encoding="utf-8").count("\n") < 600 for path in WORKFLOW_MODULES)
