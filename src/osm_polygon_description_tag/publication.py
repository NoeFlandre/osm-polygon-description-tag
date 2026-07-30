"""Non-destructive Hugging Face publication planning and execution gate.

The allowlist explicitly enumerates the artifact categories uploaded to the
public dataset repository. Symlinks, temporary files, unknown top-level
paths, stale manifests, identity-mismatching manifests, and missing card or
statistics are rejected at plan time. Execution requires the caller to
confirm the exact plan identity and re-verifies every file checksum before
invoking the ``hf`` CLI.

The default runner executes a single ``hf upload-large-folder`` command
whose ``--include`` flags are derived strictly from the plan's items (no
wildcards). Retries with bounded exponential backoff are applied only to
retryable network failures returned by ``hf``. ``KeyboardInterrupt``
escapes immediately without retry.
"""

from __future__ import annotations

import json
import subprocess
import time
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

from osm_polygon_description_tag.dataset.manifest import (
    MANIFEST_SCHEMA_VERSION,
    ManifestError,
    file_sha256,
    output_identity_for,
    read_manifest,
)
from osm_polygon_description_tag.dataset.storage import StorageError, validate_geoparquet

REPO_ID = "NoeFlandre/osm-polygon-description-tag"
_ALLOWED_TOP_LEVEL = {
    "README.md",
    "stats.json",
    "data",
    "manifests",
    "publication-state.json",
    "logs",
}

# The exact uploader-owned cache directory layout. ``hf upload-large-folder``
# creates ``<data-root>/.cache/huggingface`` while it runs to enable
# resumable uploads; this directory must be permitted locally but it must
# NEVER appear in any upload plan or include flag.
_UPLOADER_CACHE_RELATIVE = ".cache/huggingface"
_LOCAL_WORK_RELATIVE = ".work"

RETRYABLE_EXIT_CODES: frozenset[int] = frozenset({5, 429, 502, 503, 504})
DEFAULT_MAX_RETRIES = 3
DEFAULT_BACKOFF_SECONDS = 1.0
DEFAULT_BACKOFF_FACTOR = 2.0
DEFAULT_BACKOFF_CAP_SECONDS = 30.0

Runner = Callable[[list[str]], None]


class PublicationError(ValueError):
    """Raised for publication planning or execution failures."""


class PublishRetry(PublicationError):
    """Raised internally to signal a retryable upload failure."""

    def __init__(self, message: str, *, exit_code: int | None, kind: str) -> None:
        super().__init__(message)
        self.exit_code = exit_code
        self.kind = kind


@dataclass(frozen=True)
class UploadItem:
    relative_path: str
    size_bytes: int
    sha256: str


@dataclass(frozen=True)
class UploadPlan:
    repo_id: str
    data_root: str
    files: tuple[UploadItem, ...]
    identity_sha256: str

    def to_payload(self) -> dict[str, object]:
        return {
            "data_root": self.data_root,
            "files": [
                {
                    "relative_path": item.relative_path,
                    "sha256": item.sha256,
                    "size_bytes": item.size_bytes,
                }
                for item in self.files
            ],
            "repo_id": self.repo_id,
        }

    def to_json(self) -> str:
        return json.dumps(self.to_payload(), ensure_ascii=False, indent=2, sort_keys=True) + "\n"


def file_sha256_bytes(data: bytes) -> str:
    import hashlib

    return hashlib.sha256(data).hexdigest()


def _build_item(path: Path, relative_path: str) -> UploadItem:
    if path.is_symlink():
        raise PublicationError(f"symlink not allowed: {path}")
    if not path.is_file():
        raise PublicationError(f"not a regular file: {path}")
    stat = path.stat()
    return UploadItem(
        relative_path=relative_path, size_bytes=stat.st_size, sha256=file_sha256(path)
    )


def _validate_manifest(manifest_path: Path, parquet_path: Path) -> None:
    """Reject empty or placeholder manifests and require output identity match."""
    try:
        manifest = read_manifest(manifest_path)
    except ManifestError as error:
        raise PublicationError(f"invalid manifest {manifest_path}: {error}") from error
    if manifest.manifest_schema_version != MANIFEST_SCHEMA_VERSION:
        raise PublicationError(
            f"manifest uses unsupported schema version: {manifest.manifest_schema_version}"
        )
    if not parquet_path.is_file():
        raise PublicationError(f"parquet missing for manifest: {parquet_path}")
    actual_output = output_identity_for(parquet_path)
    if manifest.output != actual_output:
        raise PublicationError(f"manifest output identity does not match parquet: {manifest_path}")
    try:
        validate_geoparquet(parquet_path)
    except StorageError as error:
        raise PublicationError(f"parquet fails validation for publication: {error}") from error


def _collect_allowlisted_files(data_root: Path) -> tuple[UploadItem, ...]:
    if not data_root.is_dir() or data_root.is_symlink():
        raise PublicationError(f"data root is not a regular directory: {data_root}")
    for entry in data_root.iterdir():
        if entry.name in _ALLOWED_TOP_LEVEL:
            continue
        if entry.name == ".cache":
            # The exact uploader-owned layout ``.cache/huggingface`` is
            # permitted locally; it's checked below. Any other hidden
            # top-level entry is rejected.
            if entry.is_symlink():
                raise PublicationError(
                    f"uploader cache must be a real directory, not a symlink: {entry}"
                )
            if not entry.is_dir():
                raise PublicationError(f"uploader cache must be a directory: {entry}")
            child = entry / "huggingface"
            if not child.is_dir() or child.is_symlink():
                raise PublicationError(f"expected {child} to be a real huggingface cache directory")
            continue
        if entry.name == _LOCAL_WORK_RELATIVE:
            if entry.is_symlink() or not entry.is_dir():
                raise PublicationError(f"local work path must be a real directory: {entry}")
            continue
        if entry.name == ".DS_Store":
            if entry.is_symlink() or not entry.is_file():
                raise PublicationError(f".DS_Store must be a regular file: {entry}")
            continue
        raise PublicationError(f"unknown top-level entry: {entry}")

    items: list[UploadItem] = []

    readme = data_root / "README.md"
    stats = data_root / "stats.json"
    if not readme.is_file():
        raise PublicationError(f"missing required file: {readme}")
    if not stats.is_file():
        raise PublicationError(f"missing required file: {stats}")
    items.append(_build_item(readme, "README.md"))
    items.append(_build_item(stats, "stats.json"))

    data_dir = data_root / "data"
    if data_dir.is_dir():
        for path in sorted(data_dir.iterdir(), key=lambda entry: entry.name):
            if path.name.startswith(".") or path.suffix == ".tmp":
                raise PublicationError(f"temporary or hidden file present: {path}")
            if path.suffix != ".parquet":
                raise PublicationError(f"unexpected file in data/: {path}")
            items.append(_build_item(path, f"data/{path.name}"))
            manifest_name = path.name.removesuffix(".parquet") + ".manifest.json"
            manifest_path = data_root / "manifests" / manifest_name
            _validate_manifest(manifest_path, path)

    manifests_dir = data_root / "manifests"
    if manifests_dir.is_dir():
        for path in sorted(manifests_dir.iterdir(), key=lambda entry: entry.name):
            if path.name.startswith(".") or path.name.endswith(".tmp"):
                raise PublicationError(f"temporary or hidden file present: {path}")
            if not path.name.endswith(".manifest.json"):
                raise PublicationError(f"unexpected file in manifests/: {path}")
            items.append(_build_item(path, f"manifests/{path.name}"))

    items.sort(key=lambda item: item.relative_path)
    return tuple(items)


def create_upload_plan(data_root: Path) -> UploadPlan:
    """Build the allowlisted, identity-hashed upload plan for ``data_root``.

    The plan's ``identity_sha256`` is the SHA-256 of the canonical JSON
    payload. The caller must compare its confirmation to this exact value
    before ``execute_upload`` will run.
    """
    resolved_root = data_root.resolve(strict=False)
    items = _collect_allowlisted_files(resolved_root)
    provisional = UploadPlan(
        repo_id=REPO_ID,
        data_root=str(resolved_root),
        files=items,
        identity_sha256="",
    )
    identity = file_sha256_bytes(provisional.to_json().encode("utf-8"))
    return UploadPlan(
        repo_id=REPO_ID,
        data_root=str(resolved_root),
        files=items,
        identity_sha256=identity,
    )


def _verify_identity(plan: UploadPlan) -> None:
    for item in plan.files:
        path = Path(plan.data_root) / item.relative_path
        if path.is_symlink() or not path.is_file():
            raise PublicationError(f"artifact missing for upload: {path}")
        stat = path.stat()
        if stat.st_size != item.size_bytes:
            raise PublicationError(f"size drift for {path}")
        if file_sha256(path) != item.sha256:
            raise PublicationError(f"checksum drift for {path}")


def _build_command(plan: UploadPlan) -> list[str]:
    """Build an ``hf upload-large-folder`` command from the plan's exact items.

    ``--include`` flags are derived strictly from ``plan.files`` (in
    deterministic order). No wildcards are used; previously uploaded
    artifacts are not re-sent.
    """
    command = [
        "hf",
        "upload-large-folder",
        plan.repo_id,
        plan.data_root,
        "--repo-type",
        "dataset",
    ]
    for item in plan.files:
        command.extend(["--include", item.relative_path])
    return command


def _classify_failure(
    error: object,
) -> tuple[bool, int | None, str]:
    """Return (retryable, exit_code, kind) for a subprocess error."""
    completed = getattr(error, "completed", None)
    if completed is None and isinstance(error, subprocess.CalledProcessError):
        completed = error
    if completed is None:
        return False, None, "exception"
    returncode = getattr(completed, "returncode", None)
    if isinstance(returncode, int) and returncode in RETRYABLE_EXIT_CODES:
        return True, returncode, "exit_code"
    output = (getattr(completed, "stderr", b"") or b"").decode("utf-8", errors="replace").lower()
    if "timeout" in output:
        return True, returncode, "timeout"
    return False, returncode, "exit_code"


def _default_runner_with_retry(
    command: list[str],
    *,
    max_retries: int = DEFAULT_MAX_RETRIES,
    backoff_seconds: float = DEFAULT_BACKOFF_SECONDS,
    backoff_factor: float = DEFAULT_BACKOFF_FACTOR,
    backoff_cap_seconds: float = DEFAULT_BACKOFF_CAP_SECONDS,
    timeout: float | None = None,
    _runner: Callable[[list[str], float | None], None] | None = None,
    retry_observer: Callable[..., None] | None = None,
) -> None:
    """Default ``hf`` runner with bounded exponential backoff on retryable errors.

    ``timeout`` defaults to ``None`` (no overall timeout) so healthy resumable
    uploads are not killed at five minutes. Callers may pass a positive value
    for explicit termination.

    ``_runner`` is a private hook for tests; production code uses
    :func:`subprocess.run`. KeyboardInterrupt always escapes immediately
    without retry.
    """
    attempt = 0
    delay = backoff_seconds

    def _invoke() -> None:
        if _runner is None:
            subprocess.run(  # noqa: S603 - controlled argument array, no shell
                command,
                check=True,
                shell=False,
                timeout=timeout,
            )
            return
        _runner(command, timeout)

    while True:
        try:
            _invoke()
            return
        except KeyboardInterrupt:
            raise
        except subprocess.CalledProcessError as error:
            retryable, exit_code, kind = _classify_failure(error)
            if not retryable or attempt >= max_retries:
                raise
            attempt += 1
            bounded_delay = min(delay, backoff_cap_seconds)
            if retry_observer is not None:
                retry_observer(
                    attempt=attempt,
                    kind=kind,
                    exit_code=exit_code,
                    delay_seconds=bounded_delay,
                )
            time.sleep(bounded_delay)
            delay *= backoff_factor
        except subprocess.TimeoutExpired:
            if attempt >= max_retries:
                raise
            attempt += 1
            bounded_delay = min(delay, backoff_cap_seconds)
            if retry_observer is not None:
                retry_observer(
                    attempt=attempt,
                    kind="timeout",
                    exit_code=None,
                    delay_seconds=bounded_delay,
                )
            time.sleep(bounded_delay)
            delay *= backoff_factor


def execute_upload(
    plan: UploadPlan,
    *,
    confirmation: str | None = None,
    runner: Runner | None = None,
    timeout: float | None = None,
    retry_observer: Callable[..., None] | None = None,
) -> None:
    """Execute the upload only after the exact plan identity is confirmed.

    ``confirmation`` is compared to the freshly computed plan identity from
    the same plan instance. A wrong or missing confirmation is refused
    before any command is executed. ``timeout`` is forwarded to the default
    runner; callers that inject ``runner`` are responsible for honoring it.

    The ``--include`` list is derived strictly from the plan's items (no
    wildcards), so previously uploaded artifacts are not re-sent.
    """
    if confirmation is None:
        raise PublicationError("confirmation required (must match freshly computed plan identity)")
    if confirmation != plan.identity_sha256:
        raise PublicationError("confirmation does not match plan identity (refusing to upload)")
    _verify_identity(plan)
    command = _build_command(plan)
    if runner is None:
        try:
            _default_runner_with_retry(
                command,
                timeout=timeout,
                retry_observer=retry_observer,
            )
        except TypeError as error:
            # Compatibility for injected legacy runners used by embedders.
            if "unexpected keyword argument 'retry_observer'" not in str(error):
                raise
            _default_runner_with_retry(command, timeout=timeout)
    else:
        runner(command)


def _build_per_pbf_upload_plan(data_root: Path, source_name: str) -> UploadPlan:
    """Build an :class:`UploadPlan` for one PBF containing exactly 4 files.

    The plan items are always:

    - ``data/<stem>.parquet``
    - ``manifests/<stem>.manifest.json``
    - ``README.md``
    - ``stats.json``

    This is the single source of truth for the production and test runner
    paths. Production and tests must not diverge.
    """
    if not source_name.endswith(".osm.pbf"):
        raise PublicationError(f"invalid source name: {source_name!r}")
    stem = source_name.removesuffix(".osm.pbf")
    required = (
        data_root / "data" / f"{stem}.parquet",
        data_root / "manifests" / f"{stem}.manifest.json",
        data_root / "README.md",
        data_root / "stats.json",
    )
    for path in required:
        if not path.is_file():
            raise PublicationError(f"required file missing for per-PBF plan: {path}")
    items = tuple(_build_item(path, path.relative_to(data_root).as_posix()) for path in required)
    resolved_root = data_root.resolve(strict=False)
    provisional = UploadPlan(
        repo_id=REPO_ID,
        data_root=str(resolved_root),
        files=items,
        identity_sha256="",
    )
    identity = file_sha256_bytes(provisional.to_json().encode("utf-8"))
    return UploadPlan(
        repo_id=REPO_ID,
        data_root=str(resolved_root),
        files=items,
        identity_sha256=identity,
    )


def _build_metadata_only_upload_plan(data_root: Path) -> UploadPlan:
    """Build an :class:`UploadPlan` containing only README.md and stats.json."""
    required = (data_root / "README.md", data_root / "stats.json")
    for path in required:
        if not path.is_file():
            raise PublicationError(f"required file missing for metadata plan: {path}")
    items = tuple(_build_item(path, path.relative_to(data_root).as_posix()) for path in required)
    resolved_root = data_root.resolve(strict=False)
    provisional = UploadPlan(
        repo_id=REPO_ID,
        data_root=str(resolved_root),
        files=items,
        identity_sha256="",
    )
    identity = file_sha256_bytes(provisional.to_json().encode("utf-8"))
    return UploadPlan(
        repo_id=REPO_ID,
        data_root=str(resolved_root),
        files=items,
        identity_sha256=identity,
    )


def per_pbf_command(data_root: Path, source_name: str) -> list[str]:
    """Build the canonical per-PBF ``hf upload-large-folder`` command.

    This is the single canonical function used by both the production
    default runner and any injected test runner. Production and tests
    must not diverge on the upload contents.
    """
    plan = _build_per_pbf_upload_plan(data_root, source_name)
    return _build_command(plan)


def metadata_only_command(data_root: Path) -> list[str]:
    """Build the canonical metadata-only ``hf upload-large-folder`` command."""
    plan = _build_metadata_only_upload_plan(data_root)
    return _build_command(plan)


__all__ = [
    "REPO_ID",
    "PublicationError",
    "Runner",
    "UploadItem",
    "UploadPlan",
    "_build_metadata_only_upload_plan",
    "_build_per_pbf_upload_plan",
    "create_upload_plan",
    "execute_upload",
    "file_sha256_bytes",
    "metadata_only_command",
    "per_pbf_command",
]
