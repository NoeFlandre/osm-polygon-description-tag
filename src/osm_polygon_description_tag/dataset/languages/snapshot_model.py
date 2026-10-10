"""Snapshot manifest model: canonical identity payloads and strict validation.

The model owns what a snapshot means: its schema versions, the source-file
identity it binds, the model identity payload, and the canonical snapshot id.
Discovery, preparation and verification of the files on disk live in
``snapshot.py``.
"""

from __future__ import annotations

from dataclasses import dataclass
from functools import partial
from pathlib import Path
from typing import Final

from osm_polygon_description_tag.dataset.languages.models import (
    LINGUA_DETECTOR_NAME,
    LanguageModelIdentity,
    LanguagePolicy,
)
from osm_polygon_description_tag.dataset.languages.paths import relative_posix_path
from osm_polygon_description_tag.dataset.languages.payloads import PayloadReader, require_object
from osm_polygon_description_tag.dataset.schema import SCHEMA_VERSION
from osm_polygon_description_tag.runtime.serialization import canonical_json_text, sha256_json
from osm_polygon_description_tag.runtime.validation import validate_fingerprint

SNAPSHOT_SCHEMA_VERSION: Final = 1
SOURCE_SCHEMA_VERSION: Final = SCHEMA_VERSION


class SnapshotError(ValueError):
    """Raised when a snapshot or its immutable source binding is invalid."""


_canonical_json = canonical_json_text
_sha256_json = sha256_json


_validate_fingerprint = partial(validate_fingerprint, error=SnapshotError)


def relative_path_text(value: str | Path) -> str:
    return relative_posix_path(value, error=SnapshotError, label="source path")


def _validate_relative_path(value: str) -> None:
    if relative_path_text(value) != value:
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
        normalized = relative_path_text(relative_path)
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
    def from_payload(cls, payload: object) -> SnapshotManifest:
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


def candidate_manifest(
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


__all__ = [
    "SNAPSHOT_SCHEMA_VERSION",
    "SOURCE_SCHEMA_VERSION",
    "SnapshotError",
    "SnapshotManifest",
    "SourceFileSnapshot",
    "candidate_manifest",
    "relative_path_text",
]
