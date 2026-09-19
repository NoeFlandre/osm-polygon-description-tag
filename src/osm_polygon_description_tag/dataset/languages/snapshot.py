"""Immutable, portable input snapshots for local language runs.

A snapshot freezes the identity of every source Parquet, the detector
configuration, and the code/lock identity that produced a run. The canonical
payload deliberately excludes the absolute source root, so staging a shard on
another machine does not change its identity.
"""

import hashlib
import json
import re
from collections.abc import Iterable
from dataclasses import dataclass
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
    LINGUA_DETECTOR_NAME,
    LanguageModelIdentity,
    LanguagePolicy,
    language_model_identity,
)
from osm_polygon_description_tag.dataset.languages.paths import relative_posix_path
from osm_polygon_description_tag.dataset.languages.payloads import PayloadReader, require_object
from osm_polygon_description_tag.dataset.manifest import file_sha256
from osm_polygon_description_tag.dataset.schema import SCHEMA, SCHEMA_VERSION

SNAPSHOT_SCHEMA_VERSION: Final = 1
SNAPSHOT_FILENAME: Final = "snapshot.json"
SOURCE_SCHEMA_VERSION: Final = SCHEMA_VERSION
_FINGERPRINT_PATTERN = re.compile(r"[0-9a-f]{64}\Z")
_IGNORED_SOURCE_DIRECTORIES: Final = frozenset({"__pycache__"})
_IGNORED_SOURCE_SUFFIXES: Final = frozenset({".pyc", ".pyo"})


class SnapshotError(ValueError):
    """Raised when a snapshot or its immutable source binding is invalid."""


def _canonical_json(payload: object) -> str:
    # pragma: no mutate start - ensure_ascii=False and None are equivalent; exact bytes tested
    return json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    # pragma: no mutate end


def _sha256_json(payload: object) -> str:
    return hashlib.sha256(_canonical_json(payload).encode()).hexdigest()


def _validate_fingerprint(value: object, label: str) -> None:
    if not isinstance(value, str) or _FINGERPRINT_PATTERN.fullmatch(value) is None:
        raise SnapshotError(f"{label} must be a lowercase SHA-256 hex fingerprint")


def _relative_path_text(value: str | Path) -> str:
    return relative_posix_path(value, error=SnapshotError, label="source path")


def _validate_relative_path(value: str) -> None:
    if _relative_path_text(value) != value:
        raise SnapshotError("source relative paths must use portable POSIX spelling")


@dataclass(frozen=True, slots=True)
class SourceFileSnapshot:
    """Content identity and schema facts for one source Parquet file."""

    relative_path: str
    size_bytes: int
    sha256: str
    schema_version: int
    schema_fingerprint: str
    row_count: int

    def __post_init__(self) -> None:
        _validate_relative_path(self.relative_path)
        _validate_non_negative_int(self.size_bytes, "source size_bytes")
        _validate_fingerprint(self.sha256, "source sha256")
        if self.schema_version != SOURCE_SCHEMA_VERSION:
            raise SnapshotError(f"source schema version must be {SOURCE_SCHEMA_VERSION}")
        _validate_fingerprint(self.schema_fingerprint, "source schema fingerprint")
        _validate_non_negative_int(self.row_count, "source row_count")

    def to_payload(self) -> dict[str, object]:
        return {
            "relative_path": self.relative_path,
            "size_bytes": self.size_bytes,
            "sha256": self.sha256,
            "schema_version": self.schema_version,
            "schema_fingerprint": self.schema_fingerprint,
            "row_count": self.row_count,
        }


def _validate_non_negative_int(value: object, label: str) -> None:
    if type(value) is not int or value < 0:
        raise SnapshotError(f"{label} must be a non-negative integer")


def _source_file_from_payload(payload: object) -> SourceFileSnapshot:
    reader = require_object(payload, error=SnapshotError, label="source file")
    return SourceFileSnapshot(
        relative_path=reader.text("relative_path"),
        size_bytes=reader.integer("size_bytes"),
        sha256=reader.text("sha256"),
        schema_version=reader.integer("schema_version"),
        schema_fingerprint=reader.text("schema_fingerprint"),
        row_count=reader.integer("row_count"),
    )


def _model_payload(identity: LanguageModelIdentity) -> dict[str, object]:
    policy = identity.policy
    return {
        "detector_name": identity.detector_name,
        "library_name": identity.library_name,
        "library_version": identity.library_version,
        "language_scope": list(identity.language_scope),
        "model_repository": identity.model_repository,
        "model_filename": identity.model_filename,
        "model_revision": identity.model_revision,
        "runtime_library_name": identity.runtime_library_name,
        "runtime_library_version": identity.runtime_library_version,
        "policy": {
            "min_alphabetic_chars": policy.min_alphabetic_chars,
            "tie_epsilon": policy.tie_epsilon,
        },
        "policy_fingerprint": identity.policy_fingerprint,
        "config_fingerprint": identity.config_fingerprint,
        "binary_artifact_hash": identity.binary_artifact_hash,
        "splitter_name": identity.splitter_name,
        "splitter_revision": identity.splitter_revision,
        "splitter_languages_fingerprint": identity.splitter_languages_fingerprint,
    }


_LEGACY_MODEL_FIELDS: Final = (
    "library_name",
    "library_version",
    "language_scope",
    "policy",
    "policy_fingerprint",
    "config_fingerprint",
    "binary_artifact_hash",
)


def _legacy_model_payload(identity: LanguageModelIdentity) -> dict[str, object]:
    payload = _model_payload(identity)
    return {name: payload[name] for name in _LEGACY_MODEL_FIELDS}


def _policy_from_payload(reader: PayloadReader) -> LanguagePolicy:
    values = reader.reader("policy")
    try:
        return LanguagePolicy(
            min_alphabetic_chars=values.integer("min_alphabetic_chars"),
            tie_epsilon=values.number("tie_epsilon"),
        )
    except SnapshotError:
        raise
    except (TypeError, ValueError) as error:
        raise SnapshotError(f"invalid snapshot policy: {error}") from error


# Only ``LanguageModelIdentity``'s ``init=False`` fields can disagree with the payload.
# Every other stored field is handed to the constructor by ``_derived_identity`` and kept
# verbatim, so comparing it back to the payload compares a value with itself.
_DERIVED_MODEL_FIELDS: Final = (
    "library_name",
    "library_version",
    "policy_fingerprint",
    "config_fingerprint",
)

# The splitter identity binds through ``config_fingerprint`` and is recorded in
# full so a reader can name the splitter without recomputing a hash. It is
# verified only where it is present: a snapshot written before these keys
# existed had its id hashed over a payload without them, so there is nothing
# there to disagree with.
_OPTIONAL_DERIVED_MODEL_FIELDS: Final = (
    "splitter_name",
    "splitter_revision",
    "splitter_languages_fingerprint",
)


def _derived_identity(reader: PayloadReader, policy: LanguagePolicy) -> LanguageModelIdentity:
    try:
        return LanguageModelIdentity(
            policy=policy,
            language_scope=reader.texts("language_scope"),
            detector_name=reader.text("detector_name")
            if reader.has("detector_name")
            else LINGUA_DETECTOR_NAME,
            model_repository=_optional_text(reader, "model_repository"),
            model_filename=_optional_text(reader, "model_filename"),
            model_revision=_optional_text(reader, "model_revision"),
            runtime_library_name=_optional_text(reader, "runtime_library_name"),
            runtime_library_version=_optional_text(reader, "runtime_library_version"),
        )
    except SnapshotError:
        raise
    except (TypeError, ValueError) as error:
        raise SnapshotError(f"invalid snapshot model identity: {error}") from error


def _optional_text(reader: PayloadReader, key: str, default: str | None = None) -> str | None:
    if not reader.has(key):
        return default
    value = reader.raw(key)
    if value is not None and not isinstance(value, str):
        raise SnapshotError(f"snapshot field {key} must be a string or null")
    return value


def _verify_derived_fields(reader: PayloadReader, identity: LanguageModelIdentity) -> None:
    """Refuse a snapshot whose recorded derived fields differ from the recomputed ones."""
    for name in _DERIVED_MODEL_FIELDS:
        _verify_derived_field(reader, identity, name)
    for name in _OPTIONAL_DERIVED_MODEL_FIELDS:
        if reader.has(name):
            _verify_derived_field(reader, identity, name)


def _verify_derived_field(
    reader: PayloadReader, identity: LanguageModelIdentity, name: str
) -> None:
    if reader.text(name) != getattr(identity, name):
        raise SnapshotError(f"model identity field does not match derived value: {name}")


def _binary_artifact_hash(reader: PayloadReader) -> str | None:
    value = reader.raw("binary_artifact_hash")
    if value is not None and not isinstance(value, str):
        raise SnapshotError("snapshot field binary_artifact_hash must be a string or null")
    return value


def _validate_binary_hash(binary_hash: str | None, expected: str | None) -> None:
    if expected is None:
        if binary_hash is not None:
            raise SnapshotError(
                "binary artifact hash must remain unset until independently verified"
            )
        return
    if binary_hash is None:
        raise SnapshotError("pinned binary artifact hash is required")
    if binary_hash != expected:
        raise SnapshotError(
            "model identity field does not match derived value: binary_artifact_hash"
        )


def _validate_binary_artifact_hash(reader: PayloadReader, identity: LanguageModelIdentity) -> None:
    _validate_binary_hash(_binary_artifact_hash(reader), identity.binary_artifact_hash)


def _model_from_payload(reader: PayloadReader) -> LanguageModelIdentity:
    identity = _derived_identity(reader, _policy_from_payload(reader))
    _verify_derived_fields(reader, identity)
    _validate_binary_artifact_hash(reader, identity)
    return identity


def _validate_source_file_order(files: tuple[SourceFileSnapshot, ...]) -> None:
    if files != tuple(sorted(files, key=lambda item: item.relative_path)):
        raise SnapshotError("source files must be sorted by relative path")
    if len({item.relative_path for item in files}) != len(files):
        raise SnapshotError("source files must not contain duplicate relative paths")


@dataclass(frozen=True, slots=True)
class SnapshotManifest:
    """Frozen run identity whose hash excludes the source root path."""

    snapshot_id: str
    snapshot_schema_version: int
    source_schema_version: int
    source_files: tuple[SourceFileSnapshot, ...]
    model_identity: LanguageModelIdentity
    code_fingerprint: str
    lock_fingerprint: str

    def __post_init__(self) -> None:
        self._validate_versions()
        files = tuple(self.source_files)
        _validate_source_file_order(files)
        object.__setattr__(self, "source_files", files)
        _validate_fingerprint(self.code_fingerprint, "code fingerprint")
        _validate_fingerprint(self.lock_fingerprint, "lock fingerprint")
        _validate_fingerprint(self.snapshot_id, "snapshot id")
        if not self._matches_canonical_id():
            raise SnapshotError("snapshot id does not match canonical content")

    def _validate_versions(self) -> None:
        if self.snapshot_schema_version != SNAPSHOT_SCHEMA_VERSION:
            raise SnapshotError(
                f"unsupported snapshot schema version: {self.snapshot_schema_version!r}"
            )
        if self.source_schema_version != SOURCE_SCHEMA_VERSION:
            raise SnapshotError(f"source schema version must be {SOURCE_SCHEMA_VERSION}")
        if not isinstance(self.model_identity, LanguageModelIdentity):
            raise TypeError("model_identity must be a LanguageModelIdentity")

    @property
    def model_config_fingerprint(self) -> str:
        """Return the detector configuration fingerprint bound to this run."""
        return self.model_identity.config_fingerprint

    def source_file(self, relative_path: str | Path) -> SourceFileSnapshot:
        """Return the frozen identity of one snapshot-listed source file."""
        normalized = _relative_path_text(relative_path)
        for item in self.source_files:
            if item.relative_path == normalized:
                return item
        raise SnapshotError(f"source file is not in snapshot: {normalized}")

    def _matches_canonical_id(self) -> bool:
        if self.snapshot_id == _sha256_json(self._identity_payload()):
            return True
        return (
            self.model_identity.detector_name == LINGUA_DETECTOR_NAME
            and self.snapshot_id == _sha256_json(self._legacy_identity_payload())
        )

    def _identity_payload(self) -> dict[str, object]:
        return {
            "snapshot_schema_version": self.snapshot_schema_version,
            "source_schema_version": self.source_schema_version,
            "source_files": [item.to_payload() for item in self.source_files],
            "model_identity": _model_payload(self.model_identity),
            "code_fingerprint": self.code_fingerprint,
            "lock_fingerprint": self.lock_fingerprint,
        }

    def _legacy_identity_payload(self) -> dict[str, object]:
        return {
            "snapshot_schema_version": self.snapshot_schema_version,
            "source_schema_version": self.source_schema_version,
            "source_files": [item.to_payload() for item in self.source_files],
            "model_identity": _legacy_model_payload(self.model_identity),
            "code_fingerprint": self.code_fingerprint,
            "lock_fingerprint": self.lock_fingerprint,
        }

    def to_payload(self) -> dict[str, object]:
        return {"snapshot_id": self.snapshot_id, **self._identity_payload()}

    def to_json(self) -> str:
        return _canonical_json(self.to_payload()) + "\n"

    @classmethod
    def from_payload(cls, payload: object) -> "SnapshotManifest":
        """Rebuild a manifest from a payload, rejecting any malformed field."""
        reader = require_object(payload, error=SnapshotError, label="snapshot")
        files = tuple(_source_file_from_payload(item) for item in reader.items("source_files"))
        return cls(
            snapshot_id=reader.text("snapshot_id"),
            snapshot_schema_version=reader.integer("snapshot_schema_version"),
            source_schema_version=reader.integer("source_schema_version"),
            source_files=files,
            model_identity=_model_from_payload(reader.reader("model_identity")),
            code_fingerprint=reader.text("code_fingerprint"),
            lock_fingerprint=reader.text("lock_fingerprint"),
        )


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
    return _sha256_json({"fields": fields, "metadata": metadata})


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


def _candidate_manifest(
    source_files: tuple[SourceFileSnapshot, ...],
    identity: LanguageModelIdentity,
    code_fingerprint: str,
    lock_fingerprint: str,
) -> SnapshotManifest:
    payload = {
        "snapshot_schema_version": SNAPSHOT_SCHEMA_VERSION,
        "source_schema_version": SOURCE_SCHEMA_VERSION,
        "source_files": [item.to_payload() for item in source_files],
        "model_identity": _model_payload(identity),
        "code_fingerprint": code_fingerprint,
        "lock_fingerprint": lock_fingerprint,
    }
    return SnapshotManifest(
        snapshot_id=_sha256_json(payload),
        snapshot_schema_version=SNAPSHOT_SCHEMA_VERSION,
        source_schema_version=SOURCE_SCHEMA_VERSION,
        source_files=source_files,
        model_identity=identity,
        code_fingerprint=code_fingerprint,
        lock_fingerprint=lock_fingerprint,
    )


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
    _validate_fingerprint(code_fingerprint, "code fingerprint")
    _validate_fingerprint(lock_fingerprint, "lock fingerprint")
    identity = _resolved_model_identity(model_identity, policy, language_scope)
    candidate = _candidate_manifest(
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
        text = path.read_text(encoding="utf-8")  # pragma: no mutate - codec alias only
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
    normalized = _relative_path_text(relative_path)
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
    return _sha256_json({"files": entries})


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
