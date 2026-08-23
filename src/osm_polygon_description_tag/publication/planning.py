from __future__ import annotations

from pathlib import Path

from osm_polygon_description_tag.dataset.manifest import (
    MANIFEST_SCHEMA_VERSION,
    Manifest,
    ManifestError,
    file_sha256,
    output_identity_for,
    read_manifest,
)
from osm_polygon_description_tag.dataset.storage import StorageError, validate_geoparquet
from osm_polygon_description_tag.publication.artifacts import (
    AREA_HISTOGRAM_ARTIFACT,
    DATASET_CARD_HERO_ARTIFACT,
    DOCUMENT_ARTIFACTS,
    H3_MAP_ARTIFACT,
    METADATA_ARTIFACTS,
    VISUAL_ARTIFACTS,
)
from osm_polygon_description_tag.publication.models import (
    REPO_ID,
    PublicationError,
    UploadItem,
    UploadPlan,
)
from osm_polygon_description_tag.publication.upload import _build_command

ALLOWED_TOP_LEVEL = {
    "README.md",
    "assets",
    "data",
    "logs",
    "manifests",
    "publication-state.json",
    "stats.json",
}

# The exact uploader-owned cache directory layout. ``hf upload-large-folder``
# creates ``<data-root>/.cache/huggingface`` while it runs to enable
# resumable uploads; this directory must be permitted locally but it must
# NEVER appear in any upload plan or include flag.
_UPLOADER_CACHE_RELATIVE = ".cache/huggingface"
_LOCAL_WORK_RELATIVE = ".work"

# The exact uploader-owned asset filenames that may appear under
# ``assets/``. The allowlist below rejects every other entry to keep
# the publication surface explicit and bounded.
H3_MAP_FILENAME = H3_MAP_ARTIFACT.filename
H3_MAP_ASSET_RELATIVE = H3_MAP_ARTIFACT.relative_path
AREA_HISTOGRAM_FILENAME = AREA_HISTOGRAM_ARTIFACT.filename
AREA_HISTOGRAM_ASSET_RELATIVE = AREA_HISTOGRAM_ARTIFACT.relative_path
DATASET_CARD_HERO_FILENAME = DATASET_CARD_HERO_ARTIFACT.filename
DATASET_CARD_HERO_ASSET_RELATIVE = DATASET_CARD_HERO_ARTIFACT.relative_path
_ALLOWED_ASSET_FILES = frozenset(
    artifact.filename
    for artifact in METADATA_ARTIFACTS
    if artifact.relative_path.startswith("assets/")
)
_EMPTY_IDENTITY = ""


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


def _read_manifest_for_publication(manifest_path: Path) -> Manifest:
    try:
        return read_manifest(manifest_path)
    except ManifestError as error:
        raise PublicationError(f"invalid manifest {manifest_path}: {error}") from error


def _require_supported_manifest_version(manifest: Manifest) -> None:
    if manifest.manifest_schema_version != MANIFEST_SCHEMA_VERSION:
        raise PublicationError(
            f"manifest uses unsupported schema version: {manifest.manifest_schema_version}"
        )


def _require_matching_parquet(manifest: Manifest, manifest_path: Path, parquet_path: Path) -> None:
    if not parquet_path.is_file():
        raise PublicationError(f"parquet missing for manifest: {parquet_path}")
    actual_output = output_identity_for(parquet_path)
    if manifest.output != actual_output:
        raise PublicationError(f"manifest output identity does not match parquet: {manifest_path}")


def _validate_publication_parquet(parquet_path: Path) -> None:
    try:
        validate_geoparquet(parquet_path)
    except StorageError as error:
        raise PublicationError(f"parquet fails validation for publication: {error}") from error


def _validate_manifest(manifest_path: Path, parquet_path: Path) -> None:
    """Reject empty or placeholder manifests and require output identity match."""
    manifest = _read_manifest_for_publication(manifest_path)
    _require_supported_manifest_version(manifest)
    _require_matching_parquet(manifest, manifest_path, parquet_path)
    _validate_publication_parquet(parquet_path)


def _validate_asset_entry(entry: Path) -> UploadItem:
    rejection_checks = (
        (entry.name.startswith("."), f"hidden file under assets/ not allowed: {entry}"),
        (entry.name.endswith(".tmp"), f"temporary file under assets/ not allowed: {entry}"),
        (
            entry.name not in _ALLOWED_ASSET_FILES,
            f"unrelated file under assets/ not allowed: {entry}",
        ),
        (entry.is_symlink(), f"symlink not allowed under assets/: {entry}"),
        (not entry.is_file(), f"not a regular file under assets/: {entry}"),
    )
    for rejected, message in rejection_checks:
        if rejected:
            raise PublicationError(message)
    return _build_item(entry, f"assets/{entry.name}")


def _require_core_assets(items: list[UploadItem]) -> None:
    relative_paths = {item.relative_path for item in items}
    requirements = (
        (
            H3_MAP_ARTIFACT,
            f"assets directory must contain the H3 density map at {H3_MAP_ARTIFACT.relative_path}",
        ),
        (
            AREA_HISTOGRAM_ARTIFACT,
            "assets directory must contain the area distribution histogram at "
            f"{AREA_HISTOGRAM_ARTIFACT.relative_path}",
        ),
    )
    for artifact, message in requirements:
        if artifact.relative_path not in relative_paths:
            raise PublicationError(message)


def _sorted_asset_entries(assets_dir: Path) -> list[Path]:
    # Sorting by the entry name is the publication-order contract.  ``Path``'s
    # natural order is equivalent within one directory, so these mutants are
    # intentionally excluded as non-behavioral alternatives.
    return sorted(assets_dir.iterdir(), key=lambda path: path.name)  # pragma: no mutate


def _validate_assets_directory(assets_dir: Path) -> list[UploadItem]:
    """Validate every entry under ``assets/`` and return upload items.

    Only the exact intended filenames are permitted. Hidden files,
    temporary files, symlinks, and unrelated files are rejected. Both
    the H3 density map and the area distribution histogram must be
    present so the dataset-wide, per-PBF, and metadata-only plans all
    share the same allowlist enforcement.
    """
    if assets_dir.is_symlink():
        raise PublicationError(f"assets directory must be a real directory: {assets_dir}")
    if not assets_dir.is_dir():
        raise PublicationError(f"assets directory missing: {assets_dir}")
    items = [_validate_asset_entry(entry) for entry in _sorted_asset_entries(assets_dir)]
    _require_core_assets(items)
    return items


def _validate_assets_for_publication(data_root: Path) -> tuple[UploadItem, ...]:
    """Validate the entire ``assets/`` directory and return the canonical items.

    Returns a ``(h3_map, area_histogram, dataset_card_hero)`` tuple. Both
    the per-PBF and metadata-only plans must pass through the full
    directory validation so hidden, temporary, symlinked, and unrelated
    files are always rejected, even when the canonical files alone would
    otherwise satisfy the plan.
    """
    assets_dir = data_root / "assets"
    items = _validate_assets_directory(assets_dir)
    items_by_path = {item.relative_path: item for item in items}
    requirements = (
        (
            H3_MAP_ARTIFACT,
            f"assets directory must contain the H3 density map at {H3_MAP_ARTIFACT.relative_path}",
        ),
        (
            AREA_HISTOGRAM_ARTIFACT,
            "assets directory must contain the area distribution histogram at "
            f"{AREA_HISTOGRAM_ARTIFACT.relative_path}",
        ),
        (
            DATASET_CARD_HERO_ARTIFACT,
            "assets directory must contain the dataset card hero at "
            f"{DATASET_CARD_HERO_ARTIFACT.relative_path}",
        ),
    )
    for artifact, message in requirements:
        if artifact.relative_path not in items_by_path:
            raise PublicationError(message)
    return tuple(items_by_path[artifact.relative_path] for artifact in VISUAL_ARTIFACTS)


def _validate_data_root(data_root: Path) -> None:
    if not data_root.is_dir() or data_root.is_symlink():
        raise PublicationError(f"data root is not a regular directory: {data_root}")


def _validate_uploader_cache(entry: Path) -> None:
    if entry.is_symlink():
        raise PublicationError(f"uploader cache must be a real directory, not a symlink: {entry}")
    if not entry.is_dir():
        raise PublicationError(f"uploader cache must be a directory: {entry}")
    child = entry / "huggingface"
    if not child.is_dir() or child.is_symlink():
        raise PublicationError(f"expected {child} to be a real huggingface cache directory")


def _validate_local_work(entry: Path) -> None:
    if entry.is_symlink() or not entry.is_dir():
        raise PublicationError(f"local work path must be a real directory: {entry}")


def _validate_ds_store(entry: Path) -> None:
    if entry.is_symlink() or not entry.is_file():
        raise PublicationError(f".DS_Store must be a regular file: {entry}")


def _validate_top_level_entry(entry: Path) -> None:
    if entry.name in ALLOWED_TOP_LEVEL:
        return
    if entry.name == ".cache":
        _validate_uploader_cache(entry)
        return
    if entry.name == _LOCAL_WORK_RELATIVE:
        _validate_local_work(entry)
        return
    if entry.name == ".DS_Store":
        _validate_ds_store(entry)
        return
    raise PublicationError(f"unknown top-level entry: {entry}")


def _validate_top_level_entries(data_root: Path) -> None:
    for entry in data_root.iterdir():
        _validate_top_level_entry(entry)


def _required_document_paths(data_root: Path) -> tuple[Path, ...]:
    return tuple(data_root / artifact.relative_path for artifact in DOCUMENT_ARTIFACTS)


def _collect_required_metadata_items(data_root: Path) -> list[UploadItem]:
    items: list[UploadItem] = []
    for artifact, path in zip(DOCUMENT_ARTIFACTS, _required_document_paths(data_root), strict=True):
        if not path.is_file():
            raise PublicationError(f"missing required file: {path}")
        items.append(_build_item(path, artifact.relative_path))
    return items


def _require_assets_directory_for_plan(data_root: Path) -> Path:
    assets_dir = data_root / "assets"
    if not assets_dir.exists():
        raise PublicationError(
            f"assets directory missing for plan: {assets_dir}; the H3 density map must be generated"
        )
    if assets_dir.is_symlink() or not assets_dir.is_dir():
        raise PublicationError(
            f"assets directory must be a real directory, not a symlink or file: {assets_dir}"
        )
    return assets_dir


def _validate_data_entry(path: Path) -> UploadItem:
    if path.name.startswith(".") or path.suffix == ".tmp":
        raise PublicationError(f"temporary or hidden file present: {path}")
    if path.suffix != ".parquet":
        raise PublicationError(f"unexpected file in data/: {path}")
    return _build_item(path, f"data/{path.name}")


def _collect_data_items(data_root: Path) -> list[UploadItem]:
    data_dir = data_root / "data"
    if not data_dir.is_dir():
        return []
    items: list[UploadItem] = []
    for path in sorted(data_dir.iterdir(), key=lambda entry: entry.name):  # pragma: no mutate
        items.append(_validate_data_entry(path))
        manifest_name = path.name.removesuffix(".parquet") + ".manifest.json"
        manifest_path = data_root / "manifests" / manifest_name
        _validate_manifest(manifest_path, path)
    return items


def _validate_manifest_entry(path: Path) -> UploadItem:
    if path.name.startswith(".") or path.name.endswith(".tmp"):
        raise PublicationError(f"temporary or hidden file present: {path}")
    if not path.name.endswith(".manifest.json"):
        raise PublicationError(f"unexpected file in manifests/: {path}")
    return _build_item(path, f"manifests/{path.name}")


def _sorted_manifest_entries(manifests_dir: Path) -> list[Path]:
    return sorted(manifests_dir.iterdir(), key=lambda entry: entry.name)  # pragma: no mutate


def _collect_manifest_items(data_root: Path) -> list[UploadItem]:
    manifests_dir = data_root / "manifests"
    if not manifests_dir.is_dir():
        return []
    return [_validate_manifest_entry(path) for path in _sorted_manifest_entries(manifests_dir)]


def _collect_allowlisted_files(data_root: Path) -> tuple[UploadItem, ...]:
    _validate_data_root(data_root)
    _validate_top_level_entries(data_root)
    items = _collect_required_metadata_items(data_root)
    assets_dir = _require_assets_directory_for_plan(data_root)
    items.extend(_validate_assets_directory(assets_dir))
    items.extend(_collect_data_items(data_root))
    items.extend(_collect_manifest_items(data_root))
    return tuple(sorted(items, key=lambda item: item.relative_path))


def _finalize_upload_plan(data_root: Path, items: tuple[UploadItem, ...]) -> UploadPlan:
    resolved_root = data_root.resolve(strict=False)  # pragma: no mutate
    # ``identity_sha256`` is omitted from ``UploadPlan.to_payload`` by design;
    # changing this provisional-only field is observationally equivalent.
    # pragma: no mutate start
    provisional = UploadPlan(
        repo_id=REPO_ID,
        data_root=str(resolved_root),
        files=items,
        identity_sha256=_EMPTY_IDENTITY,
    )
    # pragma: no mutate end
    identity = file_sha256_bytes(provisional.to_json().encode("utf-8"))  # pragma: no mutate
    return UploadPlan(
        repo_id=REPO_ID,
        data_root=str(resolved_root),
        files=items,
        identity_sha256=identity,
    )


def create_upload_plan(data_root: Path) -> UploadPlan:
    """Build the allowlisted, identity-hashed upload plan for ``data_root``.

    The plan's ``identity_sha256`` is the SHA-256 of the canonical JSON
    payload. The caller must compare its confirmation to this exact value
    before ``execute_upload`` will run.
    """
    resolved_root = data_root.resolve(strict=False)  # pragma: no mutate
    items = _collect_allowlisted_files(resolved_root)
    return _finalize_upload_plan(resolved_root, items)


def _require_h3_map(data_root: Path) -> UploadItem:  # pragma: no cover - kept for callers
    """Return the canonical H3 map upload item, failing if it is missing.

    Deprecated: use :func:`_validate_assets_for_publication` which also
    requires the area distribution histogram. Kept as a public helper
    for any external caller that needs only the H3 map item.
    """
    map_path = data_root / H3_MAP_ASSET_RELATIVE
    if not map_path.is_file():
        raise PublicationError(f"required file missing for H3 map: {map_path}")
    return _build_item(map_path, H3_MAP_ASSET_RELATIVE)


def _build_per_pbf_upload_plan(data_root: Path, source_name: str) -> UploadPlan:
    """Build an :class:`UploadPlan` for one PBF containing exactly 7 files.

    The plan items are always:

    - ``data/<stem>.parquet``
    - ``manifests/<stem>.manifest.json``
    - ``README.md``
    - ``stats.json``
    - ``assets/description_polygon_density.png``
    - ``assets/area_distribution.png``
    - ``assets/dataset-card-hero.png``

    This is the single source of truth for the production and test runner
    paths. Production and tests must not diverge.
    """
    if not source_name.endswith(".osm.pbf"):
        raise PublicationError(f"invalid source name: {source_name!r}")
    stem = source_name.removesuffix(".osm.pbf")
    required = (
        data_root / "data" / f"{stem}.parquet",
        data_root / "manifests" / f"{stem}.manifest.json",
        *_required_document_paths(data_root),
    )
    for path in required:
        if not path.is_file():
            raise PublicationError(f"required file missing for per-PBF plan: {path}")
    h3_map_item, area_histogram_item, dataset_card_hero_item = _validate_assets_for_publication(
        data_root
    )
    items = (
        *(_build_item(path, path.relative_to(data_root).as_posix()) for path in required),
        h3_map_item,
        area_histogram_item,
        dataset_card_hero_item,
    )
    return _finalize_upload_plan(data_root, items)


def _build_metadata_only_upload_plan(data_root: Path) -> UploadPlan:
    """Build an :class:`UploadPlan` containing the dataset card metadata.

    The plan includes ``README.md``, ``stats.json``, and every required
    visual asset under ``assets/``.
    """
    required = _required_document_paths(data_root)
    for path in required:
        if not path.is_file():
            raise PublicationError(f"required file missing for metadata plan: {path}")
    h3_map_item, area_histogram_item, dataset_card_hero_item = _validate_assets_for_publication(
        data_root
    )
    items = (
        *(_build_item(path, path.relative_to(data_root).as_posix()) for path in required),
        h3_map_item,
        area_histogram_item,
        dataset_card_hero_item,
    )
    return _finalize_upload_plan(data_root, items)


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
