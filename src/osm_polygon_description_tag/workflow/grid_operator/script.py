"""Grid operator script responsibilities."""

from __future__ import annotations

import shlex
from pathlib import Path
from typing import Final

from osm_polygon_description_tag.dataset.languages.models import (
    CASCADE_DETECTOR_NAME,
    LINGUA_DETECTOR_NAME,
)
from osm_polygon_description_tag.dataset.languages.snapshot import SnapshotManifest
from osm_polygon_description_tag.workflow.grid_policy import (
    MAX_PROCESSING_SECONDS,
    MAX_WALLTIME_SECONDS,
)

from .models import (
    _THREAD_LIMIT_VARIABLES,
    BUNDLE_FILENAME,
    INTENT_FILENAME,
    JOB_SCRIPT_FILENAME,
    GridOperatorError,
    JobBundle,
    JobPaths,
    jobs_root,
)


def job_paths(run_dir: Path, bundle: JobBundle) -> JobPaths:
    """Return deterministic owned job paths without creating anything."""
    root = jobs_root(run_dir) / bundle.bundle_id[:32]
    return JobPaths(
        root,
        root / BUNDLE_FILENAME,
        root / INTENT_FILENAME,
        root / JOB_SCRIPT_FILENAME,
    )


def _validated_script_inputs(
    remote_project_dir: str,
    remote_source_dir: str,
    remote_run_dir: str,
    processing_seconds: int,
    batch_size: int,
    walltime_seconds: int,
    remote_bundle_dir: str | None,
    sat_model_path: str,
    glotlid_model_path: str | None,
) -> str:
    for path, label in (
        (remote_project_dir, "remote project directory"),
        (remote_source_dir, "remote source directory"),
        (remote_run_dir, "remote run directory"),
    ):
        _validate_remote_path(path, label)
    bundle_dir = remote_bundle_dir or str(Path(remote_project_dir).parent)
    _validate_remote_path(bundle_dir, "remote bundle directory")
    _validate_job_limits(processing_seconds, batch_size, walltime_seconds)
    _validate_remote_path(sat_model_path, "remote SaT model path")
    if glotlid_model_path is not None:
        _validate_remote_path(glotlid_model_path, "remote GlotLID model path")
    return bundle_dir


def render_job_script(
    bundle: JobBundle,
    *,
    remote_project_dir: str,
    remote_source_dir: str,
    remote_run_dir: str,
    processing_seconds: int = MAX_PROCESSING_SECONDS,
    batch_size: int = 512,
    walltime_seconds: int = MAX_WALLTIME_SECONDS,
    remote_bundle_dir: str | None = None,
    sat_model_path: str,
    glotlid_model_path: str | None = None,
) -> str:
    """Render the script the job runs on its allocated compute node.

    Dependency installation and inference both happen here, inside the job, so
    a frontend is never used for anything but file management and scheduling.
    """
    bundle_dir = _validated_script_inputs(
        remote_project_dir,
        remote_source_dir,
        remote_run_dir,
        processing_seconds,
        batch_size,
        walltime_seconds,
        remote_bundle_dir,
        sat_model_path,
        glotlid_model_path,
    )
    exports = "\n".join(f"export {name}=1" for name in _THREAD_LIMIT_VARIABLES)
    quoted_project = shlex.quote(remote_project_dir)
    quoted_source = shlex.quote(remote_source_dir)
    quoted_run = shlex.quote(remote_run_dir)
    quoted_shard = shlex.quote(bundle.shard)
    line_continuation = "\\" + "\n"
    sat_option = f" {line_continuation}  --sat-model-path {shlex.quote(sat_model_path)}"
    glotlid_option = ""
    if glotlid_model_path is not None:
        glotlid_option = (
            f" {line_continuation}  --glotlid-model-path {shlex.quote(glotlid_model_path)}"
        )
    scratch_prefix = f"osm-polygon-description-tag-{bundle.bundle_id[:16]}.XXXXXX"
    verify_step = _render_payload_verification(bundle_dir) if remote_bundle_dir is not None else ""
    return f"""#!/usr/bin/env bash
# Generated for bundle {bundle.bundle_id}
# Shard: {bundle.shard}
set -euo pipefail

{exports}

job_tmp_root="$(mktemp -d "${{TMPDIR:-/tmp}}/{scratch_prefix}")"
trap 'rm -rf -- "$job_tmp_root"' EXIT HUP INT TERM
export UV_PROJECT_ENVIRONMENT="$job_tmp_root/venv"
export UV_CACHE_DIR="$job_tmp_root/uv-cache"
export XDG_CACHE_HOME="$job_tmp_root/xdg-cache"
export XDG_DATA_HOME="$job_tmp_root/xdg-data"
export UV_PYTHON_INSTALL_DIR="$job_tmp_root/python"
export HF_HOME="$job_tmp_root/hf"
export HF_DATASETS_CACHE="$job_tmp_root/hf/datasets"
export HUGGINGFACE_HUB_CACHE="$job_tmp_root/hf/hub"
export TRANSFORMERS_CACHE="$job_tmp_root/hf/transformers"
export MPLCONFIGDIR="$job_tmp_root/matplotlib"
export PYTHONPYCACHEPREFIX="$job_tmp_root/pycache"
export PYTHONDONTWRITEBYTECODE=1

for variable in {" ".join(_THREAD_LIMIT_VARIABLES)}; do
  if [[ "${{!variable}}" != "1" ]]; then
    echo "thread limit $variable is not 1" >&2
    exit 64
  fi
done

cd {quoted_project}
export PATH="${{PATH}}:$HOME/.local/bin"
uv sync --frozen --no-dev --extra language
{verify_step}

uv run --no-sync osm-polygon-description-tag language run \\
  --source-root {quoted_source} \\
  --run-dir {quoted_run} \\
  --shard {quoted_shard} \\
  --batch-size {batch_size} \\
  --budget-seconds {processing_seconds}{sat_option}{glotlid_option}

uv run --no-sync osm-polygon-description-tag language validate \\
  --run-dir {quoted_run} \\
  --shard {quoted_shard}
"""


def _render_payload_verification(bundle_dir: str) -> str:
    verify_code = (
        "import sys; from pathlib import Path; "
        "from osm_polygon_description_tag.workflow.grid_operator import "
        "verify_prepared_bundle; verify_prepared_bundle(Path(sys.argv[1]))"
    )
    return f"uv run --no-sync python -c {shlex.quote(verify_code)} {shlex.quote(bundle_dir)}"


_SHELL_METACHARACTERS: Final = (
    "\x00",
    "\n",
    "\r",
    " ",
    "'",
    '"',
    "$",
    "`",
    ";",
    "&",
    "|",
    ">",
    "<",
    "(",
    ")",
    "{",
    "}",
    "[",
    "]",
    "*",
    "?",
    "!",
    "\\",
)


def _validate_remote_path(value: str, label: str) -> None:
    _require_absolute(value, label)
    if any(character in value for character in _SHELL_METACHARACTERS):
        raise GridOperatorError(f"{label} must not contain shell metacharacters")
    # ``PurePath`` drops every ``.`` component while parsing, so ``..`` is the only
    # traversal component a parsed absolute path can still carry.
    if ".." in Path(value).parts:
        raise GridOperatorError(f"{label} must not contain traversal components")


def _validate_job_limits(
    processing_seconds: object, batch_size: object, walltime_seconds: object
) -> None:
    processing = _validate_processing_seconds(processing_seconds)
    _validate_batch_size(batch_size)
    walltime = _validate_walltime_seconds(walltime_seconds)
    if processing >= walltime:
        raise GridOperatorError("processing budget must be less than walltime")


def _validate_processing_seconds(value: object) -> int:
    if type(value) is not int or not 0 < value <= MAX_PROCESSING_SECONDS:
        raise GridOperatorError(
            f"processing budget must be between 1 and {MAX_PROCESSING_SECONDS} seconds"
        )
    return value


def _validate_batch_size(value: object) -> int:
    if type(value) is not int or value < 1:
        raise GridOperatorError("batch size must be a positive integer")
    return value


def _validate_walltime_seconds(value: object) -> int:
    if type(value) is not int or not 0 < value <= MAX_WALLTIME_SECONDS:
        raise GridOperatorError(f"walltime must be between 1 and {MAX_WALLTIME_SECONDS} seconds")
    return value


def _require_absolute(value: str, label: str) -> None:
    if not isinstance(value, str) or not value:
        raise GridOperatorError(f"{label} must be a non-empty string")
    if not value.startswith("/"):
        raise GridOperatorError(f"{label} must be an absolute path")


def _glotlid_model_path_for_snapshot(
    snapshot: SnapshotManifest,
    glotlid_model_path: str | None,
) -> str | None:
    detector_name = snapshot.model_identity.detector_name
    if detector_name == CASCADE_DETECTOR_NAME:
        if glotlid_model_path is None:
            raise GridOperatorError("cascade job requires a GlotLID model path")
        return glotlid_model_path
    if detector_name == LINGUA_DETECTOR_NAME:
        if glotlid_model_path is not None:
            raise GridOperatorError("GlotLID model path requires a cascade snapshot")
        return None
    raise GridOperatorError(f"unsupported detector for Grid jobs: {detector_name!r}")
