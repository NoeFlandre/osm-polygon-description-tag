"""Fail-closed workflow preflight checks."""

import os
import shutil
import subprocess
from typing import Any, Protocol, cast

from osm_polygon_description_tag.dataset.manifest import (
    TRANSFORM_ALGORITHM_VERSION,
    current_area_policy_sha256,
)
from osm_polygon_description_tag.osm.discovery import discover_sources
from osm_polygon_description_tag.publication.models import REPO_ID
from osm_polygon_description_tag.publication.verification import _huggingface_hub
from osm_polygon_description_tag.runtime.config import Paths
from osm_polygon_description_tag.runtime.resources import (
    dataset_card_template,
    osmium_export_config,
)


class PreflightError(RuntimeError):
    """Raised when preflight verification fails before any source is touched."""


class Preflight(Protocol):
    def __call__(self) -> dict[str, object]: ...


def _probe_osmium_version(executable: str) -> str:
    """Run ``<executable> --version`` and assert the output looks like osmium."""
    binary = shutil.which(executable) or executable
    output = _run_osmium_version(binary, executable)
    _assert_osmium_output(binary, output)
    return output.splitlines()[0].strip() if output.strip() else ""


def _run_osmium_version(binary: str, executable: str) -> str:
    try:
        completed = subprocess.run(  # noqa: S603
            [binary, "--version"],
            check=True,
            shell=False,
            capture_output=True,
            text=True,
            timeout=15,
        )
    except (FileNotFoundError, subprocess.CalledProcessError) as error:
        raise PreflightError(f"osmium --version failed for {executable}: {error}") from error
    return completed.stdout or completed.stderr or ""


def _assert_osmium_output(binary: str, output: str) -> None:
    if "libosmium" not in output and "osmium version" not in output:
        raise PreflightError(
            f"osmium at {binary!r} does not look like a real osmium-tool binary: {output!r}"
        )


def _validate_local_prerequisites(
    paths: Paths,
    *,
    confirm_repo: str,
    osmium_executable: str,
    hf_executable: str,
) -> tuple[str, str]:
    try:
        paths.validate()
    except Exception as error:
        raise PreflightError(f"path validation failed: {error}") from error
    if confirm_repo != REPO_ID:
        raise PreflightError(f"--confirm-repo must equal {REPO_ID!r} (got {confirm_repo!r})")
    if shutil.which(osmium_executable) is None:
        raise PreflightError(f"osmium executable not found: {osmium_executable}")
    resolved_hf = shutil.which(hf_executable)
    if resolved_hf is None:
        raise PreflightError(f"hf executable not found: {hf_executable}")
    return _probe_osmium_version(osmium_executable), resolved_hf


def _hf_cli_identity(resolved_hf: str) -> str:
    try:
        completed = subprocess.run(  # noqa: S603
            [resolved_hf, "auth", "whoami"],
            check=True,
            shell=False,
            capture_output=True,
            text=True,
            timeout=15,
        )
    except (subprocess.CalledProcessError, subprocess.TimeoutExpired) as error:
        raise PreflightError(f"hf authentication check failed: {error}") from error
    whoami_lines = completed.stdout.splitlines()
    return whoami_lines[0].strip() if whoami_lines else ""


def _validate_data_roots(paths: Paths) -> tuple[object, ...]:
    if not os.access(paths.source_root, os.R_OK):
        raise PreflightError(f"source root is not readable: {paths.source_root}")
    if not os.access(paths.data_root, os.W_OK):
        raise PreflightError(f"data root is not writable: {paths.data_root}")
    sources = discover_sources(paths.source_root)
    if not sources:
        raise PreflightError(
            f"no source PBF files found in {paths.source_root}; nothing to publish"
        )
    return sources


def _hub_identity() -> tuple[Any, Any]:
    try:
        api = cast(Any, _huggingface_hub.HfApi)()
        identity = api.whoami()
        repo_info = api.repo_info(REPO_ID, repo_type="dataset")
    except Exception as error:
        raise PreflightError(
            f"Hub authentication/repo check failed for {REPO_ID}: {error}"
        ) from error
    if not identity:
        raise PreflightError("Hub identity is empty; check HF_TOKEN")
    repo_sha = getattr(repo_info, "sha", None)
    if not repo_sha:
        raise PreflightError(f"Hub repository {REPO_ID} returned no commit SHA")
    return api, (identity, str(repo_sha))


def _validate_hub_write(api: Any) -> None:
    try:
        api.auth_check(REPO_ID, repo_type="dataset", write=True)
    except Exception as error:
        raise PreflightError(f"Hub write permission denied for {REPO_ID}: {error}") from error


def default_preflight(
    paths: Paths,
    *,
    confirm_repo: str,
    osmium_executable: str,
    hf_executable: str,
) -> dict[str, object]:
    """Validate all local and remote prerequisites before mutation."""
    osmium_version_output, resolved_hf = _validate_local_prerequisites(
        paths,
        confirm_repo=confirm_repo,
        osmium_executable=osmium_executable,
        hf_executable=hf_executable,
    )
    whoami = _hf_cli_identity(resolved_hf)
    sources = _validate_data_roots(paths)
    api, (identity, repo_sha) = _hub_identity()
    _validate_hub_write(api)

    return {
        "osmium_executable": osmium_executable,
        "osmium_version": osmium_version_output,
        "hf_executable": resolved_hf,
        "hf_whoami": whoami,
        "hf_identity": dict(identity) if isinstance(identity, dict) else str(identity),
        "hub_repo_sha": repo_sha,
        "source_root": str(paths.source_root),
        "data_root": str(paths.data_root),
        "export_config": str(osmium_export_config()),
        "card_template": str(dataset_card_template()),
        "repo_id": REPO_ID,
        "confirm_repo": confirm_repo,
        "source_count": len(sources),
        "transform_algorithm_version": TRANSFORM_ALGORITHM_VERSION,
        "area_policy_sha256": current_area_policy_sha256(),
    }


__all__ = ["Preflight", "PreflightError", "default_preflight"]
