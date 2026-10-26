"""Immutable, portable input snapshots for local language runs.

A snapshot freezes the identity of every source Parquet, the detector
configuration, and the code/lock identity that produced a run. The canonical
payload deliberately excludes the absolute source root, so staging a shard on
another machine does not change its identity. The manifest model and its
payload rules live in ``snapshot_model.py``; this module discovers, prepares,
reads and verifies snapshots on disk.
"""

import json
from collections.abc import Iterable
from pathlib import Path
from typing import Final

import pyarrow as pa
import pyarrow.parquet as pq

from osm_polygon_description_tag.dataset.languages.atomic import atomic_write_bytes
from osm_polygon_description_tag.dataset.languages.checkpoint import (
    WORKER_LOCK_FILENAME,
    exclusive_worker_lock,
)
from osm_polygon_description_tag.dataset.languages.models import (
    DEFAULT_LANGUAGE_POLICY,
    DEFAULT_LANGUAGE_SCOPE,
    LanguageModelIdentity,
    LanguagePolicy,
    language_model_identity,
)
from osm_polygon_description_tag.dataset.languages.snapshot_model import (
    SNAPSHOT_SCHEMA_VERSION,
    SOURCE_SCHEMA_VERSION,
    SnapshotError,
    SnapshotManifest,
    SourceFileSnapshot,
    candidate_manifest,
    relative_path_text,
)
from osm_polygon_description_tag.dataset.manifest import file_sha256
from osm_polygon_description_tag.dataset.schema import SCHEMA
from osm_polygon_description_tag.runtime.serialization import sha256_json
from osm_polygon_description_tag.runtime.text_io import read_text_utf8
from osm_polygon_description_tag.runtime.validation import validate_fingerprint

SNAPSHOT_FILENAME: Final = "snapshot.json"
_IGNORED_SOURCE_DIRECTORIES: Final = frozenset({"__pycache__"})
_IGNORED_SOURCE_SUFFIXES: Final = frozenset({".pyc", ".pyo"})


def _resolved_directory(path: Path, *, label: str) -> Path:
    if path.is_symlink():
        raise SnapshotError(f"{label} must not be a symlink: {path}")
    try:
        resolved = path.resolve(strict=True)
    except OSError as error:
        raise SnapshotError(f"cannot resolve {label}: {path}: {error}") from error
    if not resolved.is_dir():
        raise SnapshotError(f"{label} is not a directory: {path}")
    return resolved


def _ensure_disjoint_output(source_root: Path, output_root: Path) -> None:
    output_resolved = output_root.resolve()
    if output_resolved == source_root or output_resolved.is_relative_to(source_root):
        raise SnapshotError("run output must be outside the immutable source directory")
    if source_root.is_relative_to(output_resolved):
        raise SnapshotError("run output must not contain the immutable source directory")


def _validate_source_schema(schema: pa.Schema, path: Path) -> None:
    if tuple(schema.names) != tuple(SCHEMA.names):
        raise SnapshotError(f"source Parquet schema is not schema {SOURCE_SCHEMA_VERSION}: {path}")
    for name in SCHEMA.names:
        expected = SCHEMA.field(name)
        actual = schema.field(name)
        if actual.type != expected.type or actual.nullable != expected.nullable:
            raise SnapshotError(f"source Parquet schema field mismatch for {name}: {path}")


def _schema_fingerprint(schema: pa.Schema) -> str:
    try:
        metadata = {key.decode(): value.hex() for key, value in (schema.metadata or {}).items()}
    except UnicodeDecodeError as error:
        raise SnapshotError("source metadata keys must be UTF-8") from error
    fields = [
        {"name": field.name, "type": str(field.type), "nullable": field.nullable}
        for field in schema
    ]
    return sha256_json({"fields": fields, "metadata": metadata})


def _resolved_source_file(root: Path, path: Path) -> Path:
    if path.is_symlink() or not path.is_file():
        raise SnapshotError(f"source file must be a regular non-symlink file: {path}")
    try:
        resolved = path.resolve(strict=True)
    except OSError as error:
        raise SnapshotError(f"cannot resolve source file: {path}: {error}") from error
    if not resolved.is_relative_to(root):
        raise SnapshotError(f"source file escapes source directory: {path}")
    return resolved


def inspect_source_file(source_root: Path, path: Path) -> SourceFileSnapshot:
    """Inspect one schema-current source Parquet without reading its rows."""
    root = _resolved_directory(source_root, label="source directory")
    resolved = _resolved_source_file(root, path)
    try:
        parquet = pq.ParquetFile(resolved)
        schema = parquet.schema_arrow
        row_count = parquet.metadata.num_rows
        size_bytes = resolved.stat().st_size
    except (OSError, pa.ArrowException) as error:
        raise SnapshotError(f"cannot inspect source Parquet {path}: {error}") from error
    _validate_source_schema(schema, resolved)
    return SourceFileSnapshot(
        relative_path=resolved.relative_to(root).as_posix(),
        size_bytes=size_bytes,
        sha256=file_sha256(resolved),
        schema_version=SOURCE_SCHEMA_VERSION,
        schema_fingerprint=_schema_fingerprint(schema),
        row_count=row_count,
    )


def _source_parquet_paths(root: Path) -> list[Path]:
    paths: list[Path] = []
    for candidate in root.rglob("*"):
        if candidate.is_symlink():
            raise SnapshotError(f"source tree contains a symlink: {candidate}")
        if candidate.is_file() and candidate.suffix == ".parquet":
            paths.append(candidate)
    return sorted(paths, key=lambda item: item.relative_to(root).as_posix())


def _source_files(source_root: Path) -> tuple[SourceFileSnapshot, ...]:
    root = _resolved_directory(source_root, label="source directory")
    return tuple(inspect_source_file(root, path) for path in _source_parquet_paths(root))


def _resolved_model_identity(
    model_identity: LanguageModelIdentity | None,
    policy: LanguagePolicy,
    language_scope: tuple[str, ...],
) -> LanguageModelIdentity:
    if model_identity is None:
        return language_model_identity(policy, language_scope=language_scope)
    if not isinstance(model_identity, LanguageModelIdentity):
        raise TypeError("model_identity must be a LanguageModelIdentity")
    return model_identity


def _require_regular_run_directory(run_dir: Path) -> None:
    """Refuse a run directory that exists but is a symlink or not a directory."""
    if run_dir.exists() and (run_dir.is_symlink() or not run_dir.is_dir()):
        raise SnapshotError(f"run directory is not a regular directory: {run_dir}")


def _verified_existing(run_dir: Path, candidate: SnapshotManifest) -> SnapshotManifest:
    existing = read_snapshot(run_dir)
    if existing != candidate:
        raise SnapshotError("immutable snapshot identity differs from requested inputs")
    return existing


def _existing_snapshot(run_dir: Path, candidate: SnapshotManifest) -> SnapshotManifest | None:
    snapshot_path = run_dir / SNAPSHOT_FILENAME
    if snapshot_path.is_symlink():
        raise SnapshotError(f"snapshot must not be a symlink: {snapshot_path}")
    if snapshot_path.exists():
        return _verified_existing(run_dir, candidate)
    if any(path.name != WORKER_LOCK_FILENAME for path in run_dir.iterdir()):
        raise SnapshotError("cannot initialize snapshot in a non-empty run directory")
    return None


def prepare_snapshot(
    source_dir: Path,
    run_dir: Path,
    *,
    code_fingerprint: str,
    lock_fingerprint: str,
    model_identity: LanguageModelIdentity | None = None,
    policy: LanguagePolicy = DEFAULT_LANGUAGE_POLICY,
    language_scope: tuple[str, ...] = DEFAULT_LANGUAGE_SCOPE,
) -> SnapshotManifest:
    """Create or verify one immutable snapshot manifest for a source directory."""
    source_root = _resolved_directory(source_dir, label="source directory")
    _ensure_disjoint_output(source_root, run_dir)
    validate_fingerprint(code_fingerprint, "code fingerprint", error=SnapshotError)
    validate_fingerprint(lock_fingerprint, "lock fingerprint", error=SnapshotError)
    identity = _resolved_model_identity(model_identity, policy, language_scope)
    candidate = candidate_manifest(
        _source_files(source_root), identity, code_fingerprint, lock_fingerprint
    )
    _require_regular_run_directory(run_dir)
    with exclusive_worker_lock(run_dir):
        existing = _existing_snapshot(run_dir, candidate)
        if existing is not None:
            return existing
        atomic_write_bytes(run_dir / SNAPSHOT_FILENAME, candidate.to_json().encode())
        return candidate


def read_snapshot(run_dir: Path) -> SnapshotManifest:
    """Read and validate the immutable snapshot stored under ``run_dir``."""
    path = run_dir / SNAPSHOT_FILENAME
    if path.is_symlink():
        raise SnapshotError(f"snapshot must not be a symlink: {path}")
    try:
        text = read_text_utf8(path)
        payload = json.loads(text)
    except (OSError, UnicodeError, json.JSONDecodeError) as error:
        raise SnapshotError(f"cannot read snapshot {path}: {error}") from error
    return SnapshotManifest.from_payload(payload)


def source_path_for(
    snapshot: SnapshotManifest,
    source_dir: Path,
    relative_path: str | Path,
) -> Path:
    """Resolve a snapshot-listed source path while rejecting traversal/escape."""
    normalized = relative_path_text(relative_path)
    snapshot.source_file(normalized)
    root = _resolved_directory(source_dir, label="source directory")
    candidate = root / Path(normalized)
    resolved = candidate.resolve()
    if candidate.is_symlink() or not resolved.is_relative_to(root):
        raise SnapshotError(f"source path escapes or uses a symlink: {normalized}")
    if not resolved.is_file():
        raise SnapshotError(f"source file is missing: {normalized}")
    return resolved


def verify_source_file(
    snapshot: SnapshotManifest,
    source_dir: Path,
    relative_path: str | Path,
) -> SourceFileSnapshot:
    """Verify one current source file against its frozen snapshot identity."""
    expected = snapshot.source_file(relative_path)
    path = source_path_for(snapshot, source_dir, expected.relative_path)
    actual = inspect_source_file(source_dir, path)
    if actual != expected:
        raise SnapshotError(f"source file does not match snapshot: {expected.relative_path}")
    return actual


def verify_all_source_files(
    snapshot: SnapshotManifest, source_dir: Path
) -> tuple[SourceFileSnapshot, ...]:
    """Verify every snapshot-listed file and reject current extra Parquets."""
    actual = _source_files(source_dir)
    if actual != snapshot.source_files:
        raise SnapshotError("source directory files do not match immutable snapshot")
    return actual


def _fingerprint_file_listing(root: Path, paths: Iterable[Path]) -> str:
    entries: list[dict[str, object]] = []
    for path in sorted(paths, key=lambda item: item.relative_to(root).as_posix()):
        if path.is_symlink() or not path.is_file():
            raise SnapshotError(f"fingerprint input must be a regular file: {path}")
        entries.append(
            {
                "relative_path": path.relative_to(root).as_posix(),
                "size_bytes": path.stat().st_size,
                "sha256": file_sha256(path),
            }
        )
    return sha256_json({"files": entries})


def _source_artifacts(source: Path) -> Iterable[Path]:
    for path in source.rglob("*"):
        if _is_ignored_source_artifact(path, source):
            continue
        if path.is_symlink():
            raise SnapshotError(f"fingerprint input must be a regular file: {path}")
        if path.is_file():
            yield path


def _is_ignored_source_artifact(path: Path, source: Path) -> bool:
    relative = path.relative_to(source)
    return bool(
        _IGNORED_SOURCE_DIRECTORIES.intersection(relative.parts)
        or path.suffix.lower() in _IGNORED_SOURCE_SUFFIXES
    )


def fingerprint_project_source(project_root: Path) -> str:
    """Fingerprint the project ``src`` tree without consulting Git state."""
    root = _resolved_directory(project_root, label="project root")
    source = root / "src"
    if source.is_symlink() or not source.is_dir():
        raise SnapshotError(f"project source directory is missing: {source}")
    return _fingerprint_file_listing(root, _source_artifacts(source))


def fingerprint_lockfile(project_root: Path) -> str:
    """Fingerprint the project ``uv.lock`` file by its exact bytes."""
    root = _resolved_directory(project_root, label="project root")
    path = root / "uv.lock"
    if path.is_symlink() or not path.is_file():
        raise SnapshotError(f"uv.lock is missing or is a symlink: {path}")
    return file_sha256(path)


def verify_project_identity(snapshot: SnapshotManifest, project_root: Path) -> None:
    """Refuse execution when the current code or lock differs from a snapshot."""
    if not isinstance(snapshot, SnapshotManifest):
        raise TypeError("snapshot must be a SnapshotManifest")
    if fingerprint_project_source(project_root) != snapshot.code_fingerprint:
        raise SnapshotError("project source does not match snapshot code fingerprint")
    if fingerprint_lockfile(project_root) != snapshot.lock_fingerprint:
        raise SnapshotError("project lockfile does not match snapshot lock fingerprint")


__all__ = [
    "SNAPSHOT_FILENAME",
    "SNAPSHOT_SCHEMA_VERSION",
    "SOURCE_SCHEMA_VERSION",
    "SnapshotError",
    "SnapshotManifest",
    "SourceFileSnapshot",
    "fingerprint_lockfile",
    "fingerprint_project_source",
    "inspect_source_file",
    "prepare_snapshot",
    "read_snapshot",
    "source_path_for",
    "verify_all_source_files",
    "verify_project_identity",
    "verify_source_file",
]
