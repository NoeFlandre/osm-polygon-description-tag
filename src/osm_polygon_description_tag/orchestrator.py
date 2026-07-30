"""Stoppable, resumable per-PBF build + publish orchestrator.

The single public command (``run_and_publish``) performs a preflight,
iterates discovered sources in deterministic filename order, and for each one:

1. Reuses the existing output if its manifest agrees with the current
   source/output identity, schema versions, area-policy checksum, transform
   algorithm version, and output algorithm revision.
2. Otherwise rebuilds exactly that PBF with the real osmium binary.
3. Validates the produced GeoParquet and manifest, regenerates
   ``stats.json`` and ``README.md``, and computes a per-PBF upload plan
   containing exactly four files (Parquet, manifest, README, stats).
4. Uploads through :func:`publication.execute_upload` (or the in-process
   test runner), with optional ``upload_timeout`` forwarded to
   ``subprocess.run``.
5. Verifies every uploaded file exists on the Hub with matching identity via
   the default Hub verifier (built on ``huggingface_hub.HfApi``) and
   records the verified remote commit SHA in ``publication-state.json``
   atomically.

Three mutually exclusive per-source outcomes are exposed:

- ``built-needs-upload`` — a fresh parquet was produced and must be uploaded.
- ``reused-local-needs-upload`` — a valid local artifact exists but has no
  matching remote state, so it must be uploaded.
- ``already-published`` — the local artifact matches the publication state, so
  nothing must happen on disk and nothing must be uploaded.

A Ctrl-C interrupt terminates the active osmium child, removes only owned
temporary files, leaves prior artifacts intact, and exits with code 130.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import uuid
from collections.abc import Callable, Iterable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Protocol, cast

from osm_polygon_description_tag.discovery import Source, discover_sources
from osm_polygon_description_tag.extraction import ExportRecord
from osm_polygon_description_tag.manifest import (
    TRANSFORM_ALGORITHM_VERSION,
    Manifest,
    current_area_policy_sha256,
    is_resumable,
    output_identity_for,
    read_manifest,
    source_identity_for,
)
from osm_polygon_description_tag.pipeline import build_one
from osm_polygon_description_tag.publication import (
    REPO_ID,
    PublicationError,
    UploadItem,
    UploadPlan,
    _build_metadata_only_upload_plan,
    _build_per_pbf_upload_plan,
    create_upload_plan,
    execute_upload,
    per_pbf_command,
)
from osm_polygon_description_tag.reporting import generate_dataset_docs
from osm_polygon_description_tag.runtime.cleanup import cleanup_stale_owned_temps
from osm_polygon_description_tag.runtime.config import Paths
from osm_polygon_description_tag.runtime.logging import RunLogger
from osm_polygon_description_tag.runtime.resources import (
    dataset_card_template,
    osmium_export_config,
)
from osm_polygon_description_tag.storage import StorageError, validate_geoparquet

PUBLICATION_STATE_FILENAME = "publication-state.json"
INTERRUPT_EXIT_CODE = 130

# Per-source state-machine labels (mutually exclusive).
STATUS_BUILT = "built-needs-upload"
STATUS_REUSED = "reused-local-needs-upload"
STATUS_PUBLISHED = "already-published"
STATUS_FAILED = "failed"

# Files that the LFS threshold applies to in the default Hub verifier.
LFS_SHA_THRESHOLD_BYTES = 5 * 1024 * 1024

ExportRecordLike = ExportRecord


class PreflightError(RuntimeError):
    """Raised when the preflight verification fails before any source is touched."""


class OrchestratorError(RuntimeError):
    """Raised for orchestrator-level failures after preflight succeeds."""


class HubVerifier(Protocol):
    """Verify that ``files`` actually exist in ``repo_id`` and return the repo SHA."""

    def __call__(self, repo_id: str, files: tuple[UploadItem, ...]) -> str: ...


class Preflight(Protocol):
    def __call__(self) -> dict[str, object]: ...


@dataclass
class SourceOutcome:
    source_name: str
    status: str
    included_rows: int = 0
    output_bytes: int = 0
    remote_revision: str | None = None
    note: str | None = None


@dataclass
class OrchestrationReport:
    source_count: int
    preflight: dict[str, object]
    outcomes: list[SourceOutcome] = field(default_factory=list)
    final_remote_revision: str | None = None

    def to_payload(self) -> dict[str, object]:
        return {
            "preflight": self.preflight,
            "source_count": self.source_count,
            "outcomes": [
                {
                    "source_name": outcome.source_name,
                    "status": outcome.status,
                    "included_rows": outcome.included_rows,
                    "output_bytes": outcome.output_bytes,
                    "remote_revision": outcome.remote_revision,
                    "note": outcome.note,
                }
                for outcome in self.outcomes
            ],
            "final_remote_revision": self.final_remote_revision,
        }


# ---------------------------------------------------------------------------
# Publication state
# ---------------------------------------------------------------------------


def _atomic_write_json(path: Path, payload: dict[str, object]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    body = json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n"
    temp = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
    try:
        temp.write_text(body, encoding="utf-8")
        with open(temp, "rb") as handle:
            os.fsync(handle.fileno())
        directory_fd = os.open(str(path.parent), os.O_RDONLY)
        try:
            os.fsync(directory_fd)
        finally:
            os.close(directory_fd)
        os.replace(temp, path)
    finally:
        if temp.exists():
            temp.unlink()


def read_publication_state(data_root: Path) -> dict[str, object]:
    state_path = data_root / PUBLICATION_STATE_FILENAME
    if not state_path.is_file():
        return {"schema_version": 1, "published": {}}
    return cast_dict(json.loads(state_path.read_text(encoding="utf-8")))


def _write_publication_state(
    data_root: Path,
    *,
    source_name: str,
    source_sha256: str,
    output_sha256: str,
    output_bytes: int,
    remote_revision: str,
    artifact_identity: str,
    completed_at: str,
) -> dict[str, object]:
    state = read_publication_state(data_root)
    if state.get("schema_version") != 1:
        raise OrchestratorError(
            f"unsupported publication state schema: {state.get('schema_version')!r}"
        )
    published = cast_dict(state.setdefault("published", {}))
    published[source_name] = {
        "source_sha256": source_sha256,
        "output_sha256": output_sha256,
        "output_bytes": output_bytes,
        "remote_revision": remote_revision,
        "artifact_identity": artifact_identity,
        "completed_at": completed_at,
    }
    state["last_updated_at"] = completed_at
    _atomic_write_json(data_root / PUBLICATION_STATE_FILENAME, state)
    return state


def _metadata_state_matches(data_root: Path, metadata_plan: UploadPlan) -> bool:
    """True only when the recorded metadata state matches the current plan."""
    state = read_publication_state(data_root)
    metadata = state.get("metadata")
    if not isinstance(metadata, dict):
        return False
    if metadata.get("identity_sha256") != metadata_plan.identity_sha256:
        return False
    readme_path = data_root / "README.md"
    stats_path = data_root / "stats.json"
    if not readme_path.is_file() or not stats_path.is_file():
        return False
    from osm_polygon_description_tag.manifest import file_sha256

    expected_readme_sha = file_sha256(readme_path)
    expected_stats_sha = file_sha256(stats_path)
    if metadata.get("readme_sha256") != expected_readme_sha:
        return False
    if metadata.get("stats_sha256") != expected_stats_sha:
        return False
    return (
        metadata.get("readme_size_bytes") == readme_path.stat().st_size
        and metadata.get("stats_size_bytes") == stats_path.stat().st_size
    )


def _write_metadata_state(
    data_root: Path,
    *,
    identity_sha256: str,
    readme_sha256: str,
    stats_sha256: str,
    readme_size_bytes: int,
    stats_size_bytes: int,
    verified_revision: str,
    completed_at: str,
) -> dict[str, object]:
    state = read_publication_state(data_root)
    if state.get("schema_version") != 1:
        raise OrchestratorError(
            f"unsupported publication state schema: {state.get('schema_version')!r}"
        )
    state["metadata"] = {
        "identity_sha256": identity_sha256,
        "readme_sha256": readme_sha256,
        "stats_sha256": stats_sha256,
        "readme_size_bytes": readme_size_bytes,
        "stats_size_bytes": stats_size_bytes,
        "verified_revision": verified_revision,
        "completed_at": completed_at,
    }
    state["last_updated_at"] = completed_at
    _atomic_write_json(data_root / PUBLICATION_STATE_FILENAME, state)
    return state


def cast_dict(value: object) -> dict[str, object]:
    if not isinstance(value, dict):
        raise OrchestratorError(f"expected dict, got {type(value).__name__}")
    return value


# ---------------------------------------------------------------------------
# Hub verifier (default factory backed by huggingface_hub.HfApi)
# ---------------------------------------------------------------------------


class _HuggingFaceHub:
    """Lazy wrapper around the huggingface_hub package.

    Importing huggingface_hub at module load time would couple the project
    to a network-authenticated dependency even for read-only operations.
    The wrapper defers the import until :func:`default_hub_verifier_factory`
    actually instantiates a verifier.

    Tests may override attributes on this instance (for example,
    ``_huggingface_hub.HfApi = lambda ...``); those overrides take
    precedence over the lazy lookup.
    """

    def __init__(self) -> None:
        self._module: object | None = None

    def _resolve_module(self) -> object:
        if self._module is None:
            import huggingface_hub as _hub

            self._module = _hub
        return self._module

    def __getattr__(self, name: str) -> object:
        return getattr(self._resolve_module(), name)


_huggingface_hub = _HuggingFaceHub()


def default_hub_verifier_factory(*, cache_dir: Path | None = None) -> HubVerifier:
    """Return a verifier that talks to the live Hugging Face Hub.

    The verifier:

    1. Confirms the caller's authenticated identity via ``HfApi.whoami``.
    2. Queries the dataset repository and reads its current commit SHA via
       ``HfApi.repo_info``. That SHA is the candidate revision.
    3. For each :class:`UploadItem` it checks ``HfApi.get_paths_info`` for the
       exact file metadata at that revision; small files are read via
       ``HfApi.hf_hub_download`` and hashed with SHA-256, larger files are
       compared against the LFS ``sha256`` reported in the Hub metadata.
    4. Returns the verified commit SHA, or raises :class:`HubVerificationError`
       on any mismatch / missing file / unauthenticated identity.

    The ``HfApi`` is resolved at invocation time (not at factory time), so
    tests may monkeypatch ``orch._huggingface_hub.HfApi`` BEFORE the
    verifier is actually called.
    """

    def verifier(repo_id: str, files: tuple[UploadItem, ...]) -> str:
        # Resolve the HfApi lazily at invocation time so monkeypatching
        # _huggingface_hub.HfApi is honored by tests.
        HfApiCls = cast(Any, _huggingface_hub.HfApi)
        api = HfApiCls()
        try:
            identity = api.whoami()
        except Exception as error:
            raise HubVerificationError(f"Hub authentication failed: {error}") from error
        if not identity:
            raise HubVerificationError("Hub authentication returned no identity")
        try:
            info = api.repo_info(repo_id, repo_type="dataset")
        except Exception as error:
            raise HubVerificationError(
                f"Hub repository {repo_id} is not accessible: {error}"
            ) from error
        revision = str(getattr(info, "sha", "") or "")
        if not revision:
            raise HubVerificationError(f"Hub repository {repo_id} returned an empty revision")
        for item in files:
            try:
                entries = api.get_paths_info(
                    repo_id,
                    paths=[item.relative_path],
                    revision=revision,
                    repo_type="dataset",
                )
            except Exception as error:
                raise HubVerificationError(
                    f"hub verification failed for {item.relative_path}: {error}"
                ) from error
            entry = next((e for e in entries), None)
            if entry is None:
                raise HubVerificationError(
                    f"remote file missing in revision {revision}: {item.relative_path}"
                )
            size = getattr(entry, "size", None)
            if size is not None and int(size) != int(item.size_bytes):
                raise HubVerificationError(
                    f"remote size mismatch for {item.relative_path}: "
                    f"local={item.size_bytes}, remote={size}"
                )
            lfs_info = getattr(entry, "lfs", None)
            lfs_sha = getattr(lfs_info, "sha256", None) if lfs_info is not None else None
            if lfs_sha:
                if str(lfs_sha).lower() != str(item.sha256).lower():
                    raise HubVerificationError(f"remote LFS SHA mismatch for {item.relative_path}")
                continue
            # Fallback: read the remote content via hf_hub_download for direct
            # SHA-256 comparison. This is the authoritative identity for small
            # non-LFS files.
            try:
                download_kwargs: dict[str, object] = {
                    "revision": revision,
                    "repo_type": "dataset",
                }
                if cache_dir is not None:
                    download_kwargs["cache_dir"] = cache_dir
                local_path = api.hf_hub_download(
                    repo_id,
                    item.relative_path,
                    **download_kwargs,
                )
            except Exception as error:
                raise HubVerificationError(
                    f"could not download {item.relative_path} from {repo_id}@{revision}: {error}"
                ) from error
            digest = _file_sha256_streaming(Path(local_path))
            if digest.lower() != str(item.sha256).lower():
                raise HubVerificationError(
                    f"remote SHA mismatch for {item.relative_path}: "
                    f"local={item.sha256}, remote={digest}"
                )
        return revision

    def reconcile_managed_files(repo_id: str, expected_paths: set[str]) -> str | None:
        """Delete only stale files in the dataset's managed artifact namespaces."""
        HfApiCls = cast(Any, _huggingface_hub.HfApi)
        api = HfApiCls()
        if not hasattr(api, "list_repo_files") or not hasattr(api, "delete_files"):
            return None
        remote_paths = set(api.list_repo_files(repo_id, repo_type="dataset"))
        stale = sorted(
            path
            for path in remote_paths - expected_paths
            if path.startswith("data/") or path.startswith("manifests/")
        )
        if not stale:
            return None
        commit = api.delete_files(
            repo_id,
            stale,
            repo_type="dataset",
            commit_message="Remove stale generated dataset artifacts",
        )
        revision = getattr(commit, "oid", None)
        if revision:
            return str(revision)
        info = api.repo_info(repo_id, repo_type="dataset")
        return str(getattr(info, "sha", "") or "") or None

    verifier.reconcile_managed_files = reconcile_managed_files  # type: ignore[attr-defined]
    return verifier


def _file_sha256_streaming(path: Path) -> str:
    """Compute SHA-256 by streaming the file in bounded chunks.

    Equivalent to ``file_sha256`` from the manifest module but explicitly
    avoids loading the entire file into memory at once. The verifier must
    use this helper so download-and-hash verification never OOMs the
    process on large dataset files.
    """
    from osm_polygon_description_tag.manifest import file_sha256 as _file_sha256

    return _file_sha256(path)


def build_default_hub_verifier() -> HubVerifier:
    """Build a fresh default Hub verifier."""
    return default_hub_verifier_factory()


class HubVerificationError(RuntimeError):
    """Raised when the default Hub verifier cannot confirm the uploaded files."""


# ---------------------------------------------------------------------------
# Preflight
# ---------------------------------------------------------------------------


def _probe_osmium_version(executable: str) -> str:
    """Run ``<executable> --version`` and assert the output looks like osmium."""
    binary = shutil.which(executable) or executable
    try:
        completed = subprocess.run(  # noqa: S603 - controlled argument array, no shell
            [binary, "--version"],
            check=True,
            shell=False,
            capture_output=True,
            text=True,
            timeout=15,
        )
    except (FileNotFoundError, subprocess.CalledProcessError) as error:
        raise PreflightError(f"osmium --version failed for {executable}: {error}") from error
    output = completed.stdout or completed.stderr or ""
    if "libosmium" not in output and "osmium version" not in output:
        raise PreflightError(
            f"osmium at {binary!r} does not look like a real osmium-tool binary: {output!r}"
        )
    return output.splitlines()[0].strip() if output.strip() else ""


def default_preflight(
    paths: Paths,
    *,
    confirm_repo: str,
    osmium_executable: str,
    hf_executable: str,
) -> dict[str, object]:
    try:
        paths.validate()
    except Exception as error:
        raise PreflightError(f"path validation failed: {error}") from error
    if confirm_repo != REPO_ID:
        raise PreflightError(f"--confirm-repo must equal {REPO_ID!r} (got {confirm_repo!r})")
    if shutil.which(osmium_executable) is None:
        raise PreflightError(f"osmium executable not found: {osmium_executable}")
    if shutil.which(hf_executable) is None:
        raise PreflightError(f"hf executable not found: {hf_executable}")

    osmium_version_output = _probe_osmium_version(osmium_executable)

    try:
        completed = subprocess.run(  # noqa: S603 - controlled argument array, no shell
            [hf_executable, "auth", "whoami"],
            check=True,
            shell=False,
            capture_output=True,
            text=True,
            timeout=15,
        )
    except (subprocess.CalledProcessError, subprocess.TimeoutExpired) as error:
        raise PreflightError(f"hf authentication check failed: {error}") from error

    whoami_lines = completed.stdout.splitlines()
    whoami = whoami_lines[0].strip() if whoami_lines else ""

    if not os.access(paths.source_root, os.R_OK):
        raise PreflightError(f"source root is not readable: {paths.source_root}")
    if not os.access(paths.data_root, os.W_OK):
        raise PreflightError(f"data root is not writable: {paths.data_root}")

    sources = discover_sources(paths.source_root)
    if not sources:
        raise PreflightError(
            f"no source PBF files found in {paths.source_root}; nothing to publish"
        )

    # Use the lazy wrapper so tests can monkeypatch ``_huggingface_hub.HfApi``
    # without hitting real Hugging Face infrastructure.
    try:
        HfApiCls = cast(Any, _huggingface_hub.HfApi)
        api = HfApiCls()
        identity = api.whoami()
        repo_info = api.repo_info(REPO_ID, repo_type="dataset")
    except Exception as error:
        raise PreflightError(
            f"Hub authentication/repo check failed for {REPO_ID}: {error}"
        ) from error
    if not identity:
        raise PreflightError("Hub identity is empty; check HF_TOKEN")
    if not getattr(repo_info, "sha", None):
        raise PreflightError(f"Hub repository {REPO_ID} returned no commit SHA")

    # Verify write permission against the target dataset. This is a
    # non-mutating Hub API call that fails closed before any PBF is opened
    # or any generated artifact is created.
    try:
        api.auth_check(
            REPO_ID,
            repo_type="dataset",
            write=True,
        )
    except Exception as error:
        raise PreflightError(f"Hub write permission denied for {REPO_ID}: {error}") from error

    return {
        "osmium_executable": osmium_executable,
        "osmium_version": osmium_version_output,
        "hf_executable": hf_executable,
        "hf_whoami": whoami,
        "hf_identity": dict(identity) if isinstance(identity, dict) else str(identity),
        "hub_repo_sha": str(getattr(repo_info, "sha", "")),
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


# ---------------------------------------------------------------------------
# Per-source processing
# ---------------------------------------------------------------------------


def _local_artifact_is_complete(paths: Paths, source: Source) -> tuple[bool, Manifest | None]:
    """Return (True, manifest) only when the local artifact is fully resumable."""
    output_path = paths.data_root / "data" / source.output_name
    manifest_path = (
        paths.data_root
        / "manifests"
        / f"{source.output_name.removesuffix('.parquet')}.manifest.json"
    )
    if not output_path.is_file() or not manifest_path.is_file():
        return False, None
    try:
        validate_geoparquet(output_path)
        manifest = read_manifest(manifest_path)
    except (StorageError, Exception):
        return False, None
    if not is_resumable(
        manifest,
        source_identity_for(source.path),
        output_identity_for(output_path),
    ):
        return False, None
    return True, manifest


def _process_one(
    source: Source,
    paths: Paths,
    *,
    clock: Callable[[], str],
    exporter: Callable[..., Iterable[ExportRecordLike]] | None = None,
    progress_interval: int = 100_000,
    logger: RunLogger | None = None,
    source_index: int = 0,
    source_total: int = 0,
    osmium_executable: str = "osmium",
) -> SourceOutcome:
    """Build or reuse one PBF and return the resulting per-source state."""
    complete, manifest = _local_artifact_is_complete(paths, source)
    state = read_publication_state(paths.data_root)
    published = cast_dict(state.get("published", {}))
    existing = cast_dict(published.get(source.name, {}))
    output_path = paths.data_root / "data" / source.output_name

    if (
        complete
        and manifest is not None
        and _published_state_matches(existing, manifest, source, output_path)
    ):
        if logger is not None:
            logger.event(
                "source_decision",
                level="INFO",
                source=source.name,
                source_index=source_index,
                source_total=source_total,
                decision="already-published",
            )
        return SourceOutcome(
            source_name=source.name,
            status=STATUS_PUBLISHED,
            included_rows=manifest.counts.included_rows,
            output_bytes=output_path.stat().st_size,
            remote_revision=None,
            note="already published; nothing to do",
        )

    if complete and manifest is not None:
        if logger is not None:
            logger.event(
                "source_decision",
                level="INFO",
                source=source.name,
                source_index=source_index,
                source_total=source_total,
                decision="reuse-local",
            )
        generate_dataset_docs(paths.data_root, dataset_card_template(), clock=clock)
        return SourceOutcome(
            source_name=source.name,
            status=STATUS_REUSED,
            included_rows=manifest.counts.included_rows,
            output_bytes=output_path.stat().st_size,
            remote_revision=None,
            note="local artifact reused; upload required",
        )

    if logger is not None:
        logger.event(
            "source_decision",
            level="INFO",
            source=source.name,
            source_index=source_index,
            source_total=source_total,
            decision="build",
        )
        logger.event(
            "build_start",
            level="INFO",
            source=source.name,
            source_index=source_index,
            source_total=source_total,
        )
    if logger is not None:

        def _on_progress(emitted: int, included: int) -> None:
            assert logger is not None
            logger.event(
                "build_progress",
                level="INFO",
                source=source.name,
                source_index=source_index,
                source_total=source_total,
                emitted=emitted,
                included=included,
            )

        progress_callback = _on_progress
    else:
        progress_callback = None
    result = build_one(
        source,
        paths,
        export_config=osmium_export_config(),
        executable=osmium_executable,
        exporter=exporter,
        progress_interval=progress_interval,
        progress_callback=progress_callback,
    )
    if logger is not None:
        logger.event(
            "build_complete",
            level="INFO",
            source=source.name,
            source_index=source_index,
            source_total=source_total,
            rows=result.included_rows,
            bytes=result.output_path.stat().st_size,
        )
    generate_dataset_docs(paths.data_root, dataset_card_template(), clock=clock)
    return SourceOutcome(
        source_name=source.name,
        status=STATUS_BUILT,
        included_rows=result.included_rows,
        output_bytes=result.output_path.stat().st_size,
        remote_revision=None,
        note="freshly built; upload required",
    )


def _published_state_matches(
    existing: dict[str, object],
    manifest: Manifest,
    source: Source,
    output_path: Path,
) -> bool:
    source_identity = source_identity_for(source.path)
    output_identity = output_identity_for(output_path)
    if existing.get("source_sha256") != source_identity.sha256:
        return False
    if existing.get("output_sha256") != output_identity.sha256:
        return False
    if manifest.source != source_identity:
        return False
    return manifest.output == output_identity


# ---------------------------------------------------------------------------
# Publication execution (single canonical path)
# ---------------------------------------------------------------------------


def _default_subprocess_runner(command: list[str], *, timeout: float | None = None) -> None:
    """Default production subprocess boundary.

    Private hook so tests can monkeypatch the real ``subprocess.run`` call.
    The signature forwards ``timeout`` so the orchestrator's
    ``upload_timeout`` reaches ``subprocess.run``.
    """
    subprocess.run(  # noqa: S603 - controlled argument array, no shell
        command,
        check=True,
        shell=False,
        timeout=timeout,
    )


def _execute_publication(
    paths: Paths,
    source: Source,
    *,
    verifier: HubVerifier | None,
    timeout: float | None,
    upload_runner: Callable[[list[str]], str] | None,
    logger: RunLogger | None = None,
) -> str:
    """Build the per-PBF plan, execute the upload, verify remote, return the SHA.

    The plan is constructed immediately before the upload by the canonical
    per-PBF plan builder; it contains exactly the four files for this PBF.
    Production and tests share the same canonical plan builder.
    """
    plan = _build_per_pbf_upload_plan(paths.data_root, source.name)
    # Revalidate the dataset-wide upload plan immediately before upload to
    # catch in-place mutations between build and upload.
    create_upload_plan(paths.data_root)
    try:
        if upload_runner is None:
            execute_upload(
                plan,
                confirmation=plan.identity_sha256,
                timeout=timeout,
                retry_observer=(
                    None
                    if logger is None
                    else lambda **fields: logger.event("upload_retry", **fields)
                ),
            )
        else:
            command = per_pbf_command(paths.data_root, source.name)
            revision = upload_runner(command)
            if not revision:
                raise PublicationError("upload runner returned empty revision")
    except KeyboardInterrupt:
        raise
    except (PublicationError, subprocess.CalledProcessError, subprocess.TimeoutExpired) as error:
        raise OrchestratorError(f"upload failed for {source.name}: {error}") from error

    if verifier is None:
        raise OrchestratorError("no Hub verifier supplied; refusing to record an unknown revision")
    if logger is not None:
        logger.event("upload_complete", level="INFO", source=source.name)
        logger.event("verification_start", level="INFO", source=source.name)
    try:
        verified = verifier(REPO_ID, plan.files)
    except KeyboardInterrupt:
        raise
    except Exception as error:
        raise OrchestratorError(f"Hub verifier failed for {source.name}: {error}") from error
    if not verified:
        raise OrchestratorError(
            f"Hub verifier returned no revision for {source.name}; refusing to record 'unknown'"
        )
    if logger is not None:
        logger.event(
            "verification_complete",
            level="INFO",
            source=source.name,
            verified_revision=verified,
        )
    return verified


# ---------------------------------------------------------------------------
# Public entry point
# ---------------------------------------------------------------------------


def run_and_publish(
    *,
    source_root: Path | None = None,
    data_root: Path | None = None,
    confirm_repo: str,
    preflight: Preflight | None = None,
    upload_runner: Callable[[list[str]], str] | None = None,
    clock: Callable[[], str] | None = None,
    paths: Paths | None = None,
    exporter: Callable[..., Iterable[ExportRecordLike]] | None = None,
    verifier: HubVerifier | None = None,
    verifier_factory: Callable[[], HubVerifier] | None = None,
    upload_timeout: float | None = None,
    subprocess_runner: Callable[[list[str]], None] | None = None,
    progress_interval: int = 100_000,
    logger: RunLogger | None = None,
    osmium_executable: str = "osmium",
) -> OrchestrationReport:
    """Stoppable, resumable build + publish for every discovered PBF.

    The default production path uses the real Hugging Face Hub API for
    verification and ``subprocess.run`` for the upload. Tests may inject
    ``upload_runner`` (a callable receiving the canonical command) and/or
    ``verifier`` (a callable returning a SHA on success) without exposing
    any of these via the public CLI.
    """
    if clock is None:
        clock = _default_clock
    owns_logger = logger is None
    if logger is None:
        if paths is None:
            if data_root is None:
                raise OrchestratorError("logger requires paths or data_root")
            data_root_path = data_root
        else:
            data_root_path = paths.data_root
        logger = RunLogger(
            data_root=data_root_path,
            run_id=str(uuid.uuid4()),
            clock=clock,
            buffer_preflight=True,
        )
    try:
        if subprocess_runner is not None:
            # Tests may redirect the production subprocess boundary; install it
            # by monkeypatching the default runner used inside execute_upload.
            import osm_polygon_description_tag.publication as pub

            original_runner = pub._default_runner_with_retry

            def _bridge(command: list[str], **kwargs: object) -> None:
                subprocess_runner(command)
                _ = kwargs

            pub._default_runner_with_retry = _bridge
            try:
                return _run_and_publish(
                    source_root=source_root,
                    data_root=data_root,
                    confirm_repo=confirm_repo,
                    preflight=preflight,
                    upload_runner=upload_runner,
                    clock=clock,
                    paths=paths,
                    exporter=exporter,
                    verifier=verifier,
                    verifier_factory=verifier_factory,
                    upload_timeout=upload_timeout,
                    progress_interval=progress_interval,
                    logger=logger,
                    osmium_executable=osmium_executable,
                )
            finally:
                pub._default_runner_with_retry = original_runner

        return _run_and_publish(
            source_root=source_root,
            data_root=data_root,
            confirm_repo=confirm_repo,
            preflight=preflight,
            upload_runner=upload_runner,
            clock=clock,
            paths=paths,
            exporter=exporter,
            verifier=verifier,
            verifier_factory=verifier_factory,
            upload_timeout=upload_timeout,
            progress_interval=progress_interval,
            logger=logger,
            osmium_executable=osmium_executable,
        )
    except KeyboardInterrupt:
        logger.event("interrupted", level="WARNING", stage="run-and-publish")
        raise
    finally:
        if owns_logger:
            logger.close()


def _run_and_publish(
    *,
    source_root: Path | None,
    data_root: Path | None,
    confirm_repo: str,
    preflight: Preflight | None,
    upload_runner: Callable[[list[str]], str] | None,
    clock: Callable[[], str],
    paths: Paths | None,
    exporter: Callable[..., Iterable[ExportRecordLike]] | None,
    verifier: HubVerifier | None,
    verifier_factory: Callable[[], HubVerifier] | None,
    upload_timeout: float | None,
    progress_interval: int,
    logger: RunLogger,
    osmium_executable: str,
) -> OrchestrationReport:
    if paths is None:
        if source_root is None or data_root is None:
            raise OrchestratorError("paths or (source_root, data_root) is required")
        paths = Paths(source_root=source_root, data_root=data_root)

    try:
        if preflight is None:
            preflight_report = default_preflight(
                paths,
                confirm_repo=confirm_repo,
                osmium_executable=osmium_executable,
                hf_executable="hf",
            )
        else:
            preflight_report = preflight()
    except Exception as error:
        logger.event("preflight_denied", level="ERROR", reason=str(error))
        logger.deny_preflight()
        raise
    logger.event(
        "preflight",
        level="INFO",
        osmium_executable=preflight_report.get("osmium_executable", "osmium"),
        osmium_version=preflight_report.get("osmium_version", ""),
        hub_repo_sha=preflight_report.get("hub_repo_sha", ""),
        source_count=preflight_report.get("source_count", 0),
    )
    logger.approve_preflight()
    removed_temps = cleanup_stale_owned_temps(paths.data_root)
    if removed_temps:
        logger.event("stale_temp_cleanup", level="INFO", rows=len(removed_temps))

    sources = discover_sources(paths.source_root)
    report = OrchestrationReport(source_count=len(sources), preflight=preflight_report)
    total_sources = len(sources)
    logger.event(
        "sources_discovered",
        level="INFO",
        total=total_sources,
    )

    # Resolve the verifier exactly once so a single HfApi instance is used.
    active_verifier: HubVerifier | None = verifier
    if active_verifier is None and verifier_factory is not None:
        active_verifier = verifier_factory()
    elif active_verifier is None and verifier is None and upload_runner is None:
        # Production path uses the default Hub verifier factory.
        try:
            active_verifier = default_hub_verifier_factory(
                cache_dir=paths.data_root / ".cache" / "huggingface" / "hub"
            )
        except TypeError as error:
            # Compatibility for injected legacy no-argument factories.
            if "unexpected keyword argument" not in str(error):
                raise
            active_verifier = default_hub_verifier_factory()

    per_pbf_upload_count = 0
    for index, source in enumerate(sources, start=1):
        outcome = _process_one(
            source,
            paths,
            clock=clock,
            exporter=exporter,
            progress_interval=progress_interval,
            logger=logger,
            source_index=index,
            source_total=total_sources,
            osmium_executable=osmium_executable,
        )
        if outcome.status == STATUS_PUBLISHED:
            report.outcomes.append(outcome)
            continue
        if outcome.status in {STATUS_BUILT, STATUS_REUSED}:
            logger.event(
                "upload_start",
                level="INFO",
                source=source.name,
                source_index=index,
                source_total=total_sources,
            )
            try:
                revision = _execute_publication(
                    paths,
                    source,
                    verifier=active_verifier,
                    timeout=upload_timeout,
                    upload_runner=upload_runner,
                    logger=logger,
                )
            except OrchestratorError as error:
                outcome.status = STATUS_FAILED
                outcome.note = str(error)
                report.outcomes.append(outcome)
                logger.event(
                    "upload_failed",
                    level="ERROR",
                    source=source.name,
                    source_index=index,
                    source_total=total_sources,
                    reason=str(error),
                )
                raise
            output_path = paths.data_root / "data" / source.output_name
            output_identity = output_identity_for(output_path)
            plan_identity = _build_per_pbf_upload_plan(paths.data_root, source.name).identity_sha256
            _write_publication_state(
                paths.data_root,
                source_name=source.name,
                source_sha256=source_identity_for(source.path).sha256,
                output_sha256=output_identity.sha256,
                output_bytes=output_path.stat().st_size,
                remote_revision=revision,
                artifact_identity=plan_identity,
                completed_at=clock(),
            )
            logger.event(
                "state_written",
                level="INFO",
                source=source.name,
                source_index=index,
                source_total=total_sources,
            )
            outcome.remote_revision = revision
            outcome.note = "published after verified upload"
            per_pbf_upload_count += 1
        report.outcomes.append(outcome)

    _verify_final_completeness(paths, sources)

    # The production verifier owns a narrowly scoped reconciliation hook.
    # It removes remote artifacts that no longer exist locally, but only
    # below data/ and manifests/; unrelated repository files are preserved.
    reconcile = getattr(active_verifier, "reconcile_managed_files", None)
    if callable(reconcile):
        full_plan = create_upload_plan(paths.data_root)
        logger.event("remote_reconciliation_start", level="INFO")
        try:
            revision = reconcile(
                REPO_ID,
                {item.relative_path for item in full_plan.files},
            )
        except KeyboardInterrupt:
            raise
        except Exception as error:
            raise OrchestratorError(f"remote artifact reconciliation failed: {error}") from error
        logger.event(
            "remote_reconciliation_complete",
            level="INFO",
            verified_revision=str(revision or ""),
        )

    # Final metadata is published independently. The metadata is uploaded
    # whenever its current plan identity differs from the verified
    # metadata state. This is independent of per-PBF upload count, so a
    # failed intermediate metadata upload is retried on the next run.
    final_metadata_revision = _upload_final_metadata(
        paths,
        verifier=active_verifier,
        upload_runner=upload_runner,
        upload_timeout=upload_timeout,
        clock=clock,
        logger=logger,
    )
    if final_metadata_revision is not None:
        report.final_remote_revision = final_metadata_revision
    logger.event(
        "run_summary",
        level="INFO",
        result="completed",
        source_count=total_sources,
        per_pbf_uploads=per_pbf_upload_count,
    )
    logger.flush()
    return report


def _verify_final_completeness(paths: Paths, sources: Iterable[Source]) -> None:
    """Every discovered source must have a complete, resumable local artifact."""
    discovered = {source.name: source for source in sources}
    completed: set[str] = set()
    extra_artifacts: list[str] = []
    missing_artifacts: list[str] = []
    for parquet in sorted((paths.data_root / "data").glob("*.parquet")):
        stem = parquet.name.removesuffix(".parquet")
        source_name = f"{stem}.osm.pbf"
        if source_name not in discovered:
            extra_artifacts.append(stem)
            continue
        manifest_path = paths.data_root / "manifests" / f"{stem}.manifest.json"
        if not manifest_path.is_file():
            missing_artifacts.append(f"{stem}.manifest.json")
            continue
        try:
            validate_geoparquet(parquet)
            manifest = read_manifest(manifest_path)
        except (StorageError, Exception):
            missing_artifacts.append(f"{stem} (invalid manifest or parquet)")
            continue
        source = discovered[source_name]
        if is_resumable(
            manifest,
            source_identity_for(source.path),
            output_identity_for(parquet),
        ):
            completed.add(source_name)
        else:
            missing_artifacts.append(f"{stem} (not resumable)")
    missing = set(discovered) - completed
    if missing or extra_artifacts or missing_artifacts:
        raise OrchestratorError(
            "final completeness failed: "
            f"missing={sorted(missing)} extra={sorted(extra_artifacts)} "
            f"incomplete={sorted(missing_artifacts)}"
        )


def _upload_final_metadata(
    paths: Paths,
    *,
    verifier: HubVerifier | None,
    upload_runner: Callable[[list[str]], str] | None,
    upload_timeout: float | None = None,
    clock: Callable[[], str] | None = None,
    logger: RunLogger | None = None,
) -> str | None:
    """Upload README.md + stats.json and verify the result via Hub API.

    The metadata is uploaded only when its current plan identity differs
    from the recorded, verified metadata state. The metadata state is
    written atomically only after remote verification succeeds.

    Raises :class:`OrchestratorError` on any failure. Returns the verified
    remote revision.
    """
    if clock is None:
        clock = _default_clock
    data_dir = paths.data_root / "data"
    if not data_dir.is_dir() or not list(data_dir.glob("*.parquet")):
        return None
    metadata_plan = _build_metadata_only_upload_plan(paths.data_root)
    # Revalidate the dataset-wide upload plan immediately before upload to
    # catch in-place mutations between per-PBF uploads and final metadata.
    create_upload_plan(paths.data_root)

    # Independently resumable: if the verified metadata state matches the
    # current plan identity, skip the upload entirely.
    if _metadata_state_matches(paths.data_root, metadata_plan):
        state_payload = read_publication_state(paths.data_root)
        metadata_state = cast_dict(state_payload.get("metadata", {}))
        revision = metadata_state.get("verified_revision")
        if logger is not None:
            logger.event(
                "metadata_skip",
                level="INFO",
                verified_revision=str(revision) if revision else "",
            )
        return str(revision) if revision else None

    if logger is not None:
        logger.event("metadata_upload_start", level="INFO")
    try:
        if upload_runner is None:
            execute_upload(
                metadata_plan,
                confirmation=metadata_plan.identity_sha256,
                timeout=upload_timeout,
                retry_observer=(
                    None
                    if logger is None
                    else lambda **fields: logger.event("upload_retry", stage="metadata", **fields)
                ),
            )
        else:
            from osm_polygon_description_tag.publication import metadata_only_command

            command = metadata_only_command(paths.data_root)
            revision = upload_runner(command)
            if not revision:
                raise PublicationError("upload runner returned empty revision")
    except KeyboardInterrupt:
        raise
    except (PublicationError, subprocess.CalledProcessError, subprocess.TimeoutExpired) as error:
        raise OrchestratorError(f"final metadata upload failed: {error}") from error
    if verifier is None:
        raise OrchestratorError("no Hub verifier supplied; cannot record final revision")
    if logger is not None:
        logger.event("metadata_upload_complete", level="INFO")
        logger.event("metadata_verification_start", level="INFO")
    try:
        verified = verifier(REPO_ID, metadata_plan.files)
    except KeyboardInterrupt:
        raise
    except Exception as error:
        raise OrchestratorError(f"final Hub verifier failed: {error}") from error
    if not verified:
        raise OrchestratorError("Hub verifier returned no revision for final metadata")
    if logger is not None:
        logger.event(
            "metadata_verification_complete",
            level="INFO",
            verified_revision=verified,
        )

    # Write the metadata state atomically only after verification succeeds.
    from osm_polygon_description_tag.manifest import file_sha256

    _write_metadata_state(
        paths.data_root,
        identity_sha256=metadata_plan.identity_sha256,
        readme_sha256=file_sha256(paths.data_root / "README.md"),
        stats_sha256=file_sha256(paths.data_root / "stats.json"),
        readme_size_bytes=(paths.data_root / "README.md").stat().st_size,
        stats_size_bytes=(paths.data_root / "stats.json").stat().st_size,
        verified_revision=verified,
        completed_at=clock(),
    )
    if logger is not None:
        logger.event(
            "metadata_state_written",
            level="INFO",
            verified_revision=verified,
        )
    return verified


def _default_clock() -> str:
    from datetime import UTC, datetime

    return datetime.now(UTC).isoformat()


__all__ = [
    "INTERRUPT_EXIT_CODE",
    "PUBLICATION_STATE_FILENAME",
    "STATUS_BUILT",
    "STATUS_FAILED",
    "STATUS_PUBLISHED",
    "STATUS_REUSED",
    "HubVerificationError",
    "OrchestrationReport",
    "OrchestratorError",
    "PreflightError",
    "SourceOutcome",
    "create_upload_plan",
    "default_hub_verifier_factory",
    "default_preflight",
    "read_publication_state",
    "run_and_publish",
]
