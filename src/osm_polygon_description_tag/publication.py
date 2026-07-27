"""Non-destructive Hugging Face publication planning and execution gate.

The allowlist explicitly enumerates the four artifact categories uploaded to
the public dataset repository. Symlinks, temporary files, unknown top-level
paths, stale manifests, identity-mismatching manifests, and missing card or
statistics are rejected at plan time. Execution requires the caller to confirm
the exact plan identity and re-verifies every file checksum before invoking
the ``hf`` CLI.

The default runner executes a single ``hf upload-large-folder`` command whose
``--include`` flags restrict the upload to:

- ``README.md``
- ``stats.json``
- ``data/*.parquet``
- ``manifests/*.manifest.json``

A custom runner may be injected for tests. Retries with bounded exponential
backoff are applied only to retryable network failures returned by ``hf``.
"""

from __future__ import annotations

import json
import subprocess
import time
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

from osm_polygon_description_tag.manifest import (
    MANIFEST_SCHEMA_VERSION,
    ManifestError,
    file_sha256,
    output_identity_for,
    read_manifest,
)
from osm_polygon_description_tag.storage import StorageError, validate_geoparquet

REPO_ID = "NoeFlandre/osm-polygon-description-tag"
_ALLOWED_TOP_LEVEL = {
    "README.md",
    "stats.json",
    "data",
    "manifests",
    "publication-state.json",
}
_UPLOAD_COMMAND_TEMPLATE: tuple[str, ...] = (
    "hf",
    "upload-large-folder",
    REPO_ID,
    "{data_root}",
    "--repo-type",
    "dataset",
    "--include",
    "README.md",
    "--include",
    "stats.json",
    "--include",
    "data/*.parquet",
    "--include",
    "manifests/*.manifest.json",
)

RETRYABLE_EXIT_CODES: frozenset[int] = frozenset({5, 429, 502, 503, 504})
RETRYABLE_TIMEOUT = frozenset({"timeout"})
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
        if entry.name not in _ALLOWED_TOP_LEVEL:
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


def file_sha256_bytes(data: bytes) -> str:
    import hashlib

    return hashlib.sha256(data).hexdigest()


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
    return [piece.format(data_root=plan.data_root) for piece in _UPLOAD_COMMAND_TEMPLATE]


def _classify_failure(
    error: object,
) -> tuple[bool, int | None, str]:
    """Return (retryable, exit_code, kind) for a subprocess error."""
    completed = getattr(error, "completed", None)
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
) -> None:
    """Default ``hf`` runner with bounded exponential backoff on retryable errors.

    ``timeout`` defaults to ``None`` (no overall timeout) so healthy resumable
    uploads are not killed at five minutes. Callers may pass a positive value
    for explicit termination.

    ``_runner`` is a private hook for tests; production code uses
    :func:`subprocess.run`.
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
            # Ctrl-C must never be retried.
            raise
        except subprocess.CalledProcessError as error:
            retryable, exit_code, kind = _classify_failure(error)
            if not retryable or attempt >= max_retries:
                raise
            attempt += 1
            time.sleep(min(delay, backoff_cap_seconds))
            delay *= backoff_factor
        except subprocess.TimeoutExpired as error:
            if attempt >= max_retries:
                raise
            attempt += 1
            time.sleep(min(delay, backoff_cap_seconds))
            delay *= backoff_factor
            _ = error


def execute_upload(
    plan: UploadPlan,
    *,
    confirmation: str | None = None,
    runner: Runner | None = None,
) -> None:
    """Execute the upload only after the exact plan identity is confirmed.

    ``confirmation`` is compared to the freshly computed plan identity from
    :func:`create_upload_plan`. A wrong or missing confirmation is refused
    before any command is executed.
    """
    if confirmation is None:
        raise PublicationError("confirmation required (must match freshly computed plan identity)")
    if confirmation != plan.identity_sha256:
        raise PublicationError("confirmation does not match plan identity (refusing to upload)")
    _verify_identity(plan)
    command = _build_command(plan)
    if runner is None:
        _default_runner_with_retry(command)
    else:
        runner(command)


__all__ = [
    "REPO_ID",
    "PublicationError",
    "Runner",
    "UploadItem",
    "UploadPlan",
    "create_upload_plan",
    "execute_upload",
    "file_sha256_bytes",
]
