from __future__ import annotations

from pathlib import Path

from osm_polygon_description_tag.dataset.manifest import (
    MANIFEST_SCHEMA_VERSION,
    ManifestError,
    file_sha256,
    output_identity_for,
    read_manifest,
)
from osm_polygon_description_tag.dataset.storage import StorageError, validate_geoparquet
from osm_polygon_description_tag.publication.models import (
    REPO_ID,
    PublicationError,
    UploadItem,
    UploadPlan,
)
from osm_polygon_description_tag.publication.upload import _build_command

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
