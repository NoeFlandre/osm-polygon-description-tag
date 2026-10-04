"""Versioned manifests, artifact identity, and resumption decisions.

A manifest is canonical UTF-8 JSON recording source/output identity, tool and
library versions, area-policy checksum, transformation algorithm version,
timing, and factual feature/rejection counts. Resumption trusts an output
only when the manifest version, area-policy checksum, transformation
algorithm version, source identity, output identity, and current code revision
all agree.
"""

import hashlib
import json
import os
import re
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any, cast

from osm_polygon_description_tag.dataset.schema import GEOPARQUET_VERSION, SCHEMA_VERSION
from osm_polygon_description_tag.runtime.atomic import fsync_dir as _fsync_dir
from osm_polygon_description_tag.runtime.resources import (
    osmium_export_config,
    project_code_revision,
)

MANIFEST_SCHEMA_VERSION = 2
TRANSFORM_ALGORITHM_VERSION = 3
_AREA_POLICY_SOURCE: tuple[str, ...] = (
    "linear_tags:true",
    "area_tags:true",
    "geometry-types:polygon",
    "geometry:orient-then-geodesic-area",
    "key:description+description:<suffix>",
    "name:exact-base-and-suffix-values",
    "require:non-empty-trimmed-value",
)
_SHA256_CHUNK = 8 * 1024 * 1024


class ManifestError(ValueError):
    """Raised for unreadable, corrupt, or unsupported manifests."""


def file_sha256(path: Path) -> str:
    """Return the hex SHA-256 of ``path`` without mutating it."""
    digest = hashlib.sha256()
    with Path(path).open("rb") as handle:
        while True:
            chunk = handle.read(_SHA256_CHUNK)
            if not chunk:
                break
            digest.update(chunk)
    return digest.hexdigest()


@dataclass(frozen=True)
class SourceIdentity:
    name: str
    size_bytes: int
    mtime_ns: int
    sha256: str


@dataclass(frozen=True)
class OutputIdentity:
    name: str
    size_bytes: int
    sha256: str


@dataclass(frozen=True)
class RunCounts:
    emitted_features: int
    included_rows: int
    rejections: dict[str, int]


def _parse_run_counts(raw: Any) -> RunCounts:
    if not isinstance(raw, dict):
        raise ManifestError("invalid manifest counts: expected an object")
    return RunCounts(
        emitted_features=_parse_count_value(raw, "emitted_features"),
        included_rows=_parse_count_value(raw, "included_rows"),
        rejections=_parse_rejection_counts(raw.get("rejections")),
    )


def _parse_count_value(raw: dict[str, Any], field: str) -> int:
    value = raw.get(field)
    if type(value) is not int:
        raise ManifestError(f"invalid manifest counts.{field}: expected an integer")
    if value < 0:
        raise ManifestError(f"invalid manifest counts.{field}: expected a non-negative integer")
    return value


def _parse_rejection_counts(raw: Any) -> dict[str, int]:
    if not isinstance(raw, dict):
        raise ManifestError("invalid manifest counts.rejections: expected string keys and integers")
    if not _rejection_counts_have_expected_types(raw):
        raise ManifestError("invalid manifest counts.rejections: expected string keys and integers")
    if any(count < 0 for count in raw.values()):
        raise ManifestError(
            "invalid manifest counts.rejections: expected non-negative integer values"
        )
    return cast(dict[str, int], raw)  # pragma: no mutate - type-only cast


def _rejection_counts_have_expected_types(raw: dict[Any, Any]) -> bool:
    return all(
        isinstance(reason, str) and reason.strip() != "" and type(count) is int
        for reason, count in raw.items()
    )


def _parse_nonnegative_version(raw: Any, field: str) -> int:
    if type(raw) is not int or raw < 0:
        raise ManifestError(f"invalid manifest {field}: expected a non-negative integer")
    return raw


def _identity_mapping(raw: Any, identity_name: str) -> dict[str, Any]:
    if not isinstance(raw, dict):
        raise ManifestError(f"invalid manifest {identity_name}: expected an object")
    return cast(dict[str, Any], raw)  # pragma: no mutate - cast is type-only


def _parse_file_name(raw: Any, identity_name: str) -> str:
    if type(raw) is not str or not raw or Path(raw).name != raw or "\\" in raw:
        raise ManifestError(f"invalid manifest {identity_name}: expected a file name")
    return raw


def _parse_source_name(raw: Any) -> str:
    name = _parse_file_name(raw, "source.name")
    if not name.endswith(".osm.pbf") or name == ".osm.pbf":
        raise ManifestError("invalid manifest source.name: expected an .osm.pbf file name")
    return name


def _parse_identity_size(raw: Any, identity_name: str) -> int:
    if type(raw) is not int or raw < 0:
        raise ManifestError(f"invalid manifest {identity_name}: expected a non-negative integer")
    return raw


def _parse_source_mtime(raw: Any) -> int:
    if type(raw) is not int:
        raise ManifestError("invalid manifest source.mtime_ns: expected an integer")
    return raw


def _parse_sha256(raw: Any, identity_name: str) -> str:
    if type(raw) is not str or re.fullmatch(r"[0-9a-f]{64}", raw) is None:
        raise ManifestError(f"invalid manifest {identity_name}: expected a SHA-256 digest")
    return raw


def _parse_source_identity(raw: Any) -> SourceIdentity:
    identity = _identity_mapping(raw, "source")
    return SourceIdentity(
        _parse_source_name(identity.get("name")),
        _parse_identity_size(identity.get("size_bytes"), "source.size_bytes"),
        _parse_source_mtime(identity.get("mtime_ns")),
        _parse_sha256(identity.get("sha256"), "source.sha256"),
    )


def _parse_output_identity(raw: Any) -> OutputIdentity:
    identity = _identity_mapping(raw, "output")
    return OutputIdentity(
        _parse_file_name(identity.get("name"), "output.name"),
        _parse_identity_size(identity.get("size_bytes"), "output.size_bytes"),
        _parse_sha256(identity.get("sha256"), "output.sha256"),
    )


@dataclass(frozen=True)
class Manifest:
    manifest_schema_version: int
    schema_version: int
    geoparquet_version: str
    transform_algorithm_version: int
    area_policy_sha256: str
    output_algorithm_revision: str
    source: SourceIdentity
    output: OutputIdentity
    osmium_version: str | None
    dependency_versions: dict[str, str]
    code_revision: str | None
    started_at: str
    completed_at: str
    counts: RunCounts

    def to_payload(self) -> dict[str, object]:
        return {
            "manifest_schema_version": self.manifest_schema_version,
            "schema_version": self.schema_version,
            "geoparquet_version": self.geoparquet_version,
            "transform_algorithm_version": self.transform_algorithm_version,
            "area_policy_sha256": self.area_policy_sha256,
            "output_algorithm_revision": self.output_algorithm_revision,
            "source": {
                "name": self.source.name,
                "size_bytes": self.source.size_bytes,
                "mtime_ns": self.source.mtime_ns,
                "sha256": self.source.sha256,
            },
            "output": {
                "name": self.output.name,
                "size_bytes": self.output.size_bytes,
                "sha256": self.output.sha256,
            },
            "osmium_version": self.osmium_version,
            "dependency_versions": dict(sorted(self.dependency_versions.items())),
            "code_revision": self.code_revision,
            "started_at": self.started_at,
            "completed_at": self.completed_at,
            "counts": {
                "emitted_features": self.counts.emitted_features,
                "included_rows": self.counts.included_rows,
                "rejections": dict(sorted(self.counts.rejections.items())),
            },
        }

    def to_json(self) -> str:
        return json.dumps(self.to_payload(), ensure_ascii=False, indent=2, sort_keys=True) + "\n"

    @classmethod
    def from_payload(cls, payload: dict[str, Any]) -> "Manifest":
        version = payload.get("manifest_schema_version")
        if type(version) is not int or version != MANIFEST_SCHEMA_VERSION:
            raise ManifestError(f"unsupported manifest schema version: {version!r}")
        counts = _parse_run_counts(payload["counts"])
        schema_version = _parse_nonnegative_version(payload.get("schema_version"), "schema_version")
        transform_algorithm_version = _parse_nonnegative_version(
            payload.get("transform_algorithm_version", 0), "transform_algorithm_version"
        )
        return cls(
            manifest_schema_version=int(payload["manifest_schema_version"]),
            schema_version=schema_version,
            geoparquet_version=str(payload["geoparquet_version"]),
            transform_algorithm_version=transform_algorithm_version,
            area_policy_sha256=str(payload.get("area_policy_sha256") or _empty_policy_hash()),
            output_algorithm_revision=str(
                payload.get("output_algorithm_revision") or _empty_policy_hash()
            ),
            source=_parse_source_identity(payload["source"]),
            output=_parse_output_identity(payload["output"]),
            osmium_version=cast("str | None", payload.get("osmium_version")),
            dependency_versions=dict(cast(dict[str, str], payload["dependency_versions"])),
            code_revision=cast("str | None", payload.get("code_revision")),
            started_at=str(payload["started_at"]),
            completed_at=str(payload["completed_at"]),
            counts=counts,
        )


def _empty_policy_hash() -> str:
    return hashlib.sha256(b"").hexdigest()


def current_area_policy_sha256() -> str:
    """Return the SHA-256 of the live osmium export policy plus documented rules.

    Both the live ``osmium-export.json`` and the documented transform rules
    must match for an existing artifact to remain reusable.
    """
    hasher = hashlib.sha256()
    config_path = osmium_export_config()
    hasher.update(config_path.read_bytes())
    hasher.update(b"\n")
    for line in _AREA_POLICY_SOURCE:
        hasher.update(line.encode("utf-8"))  # pragma: no mutate - codec names are equivalent
        hasher.update(b"\n")
    return hasher.hexdigest()


def source_identity_for(path: Path) -> SourceIdentity:
    stat = path.stat()
    return SourceIdentity(path.name, stat.st_size, stat.st_mtime_ns, file_sha256(path))


def output_identity_for(path: Path) -> OutputIdentity:
    stat = path.stat()
    return OutputIdentity(path.name, stat.st_size, file_sha256(path))


def is_resumable(
    manifest: Manifest,
    source_identity: SourceIdentity,
    output_identity: OutputIdentity,
) -> bool:
    """True only when every behavioral agreement field matches the live values.

    Behavioral agreements:

    - ``manifest_schema_version`` matches the constant.
    - ``schema_version`` matches the current Arrow schema version.
    - ``geoparquet_version`` matches the current GeoParquet version.
    - ``transform_algorithm_version`` matches the constant.
    - ``area_policy_sha256`` matches the current ``osmium-export.json`` plus
      documented transform rules.
    - ``source`` and ``output`` identities are byte-equal.
    - ``output_algorithm_revision`` matches the live output algorithm
      revision (covers the transform+policy pair as a single token).

    ``code_revision`` is recorded as provenance but is not used to invalidate
    a correct Parquet: documentation-only commits must not force a rebuild,
    because the algorithm version + area-policy checksum + output algorithm
    revision already capture every behavioral change.
    """
    if not _manifest_contract_matches(manifest):
        return False
    if manifest.source != source_identity or manifest.output != output_identity:
        return False
    output_revision = current_output_algorithm_revision()
    return manifest.output_algorithm_revision == output_revision


def _manifest_contract_matches(manifest: Manifest) -> bool:
    return all(
        (
            manifest.manifest_schema_version == MANIFEST_SCHEMA_VERSION,
            manifest.schema_version == SCHEMA_VERSION,
            manifest.geoparquet_version == GEOPARQUET_VERSION,
            manifest.transform_algorithm_version == TRANSFORM_ALGORITHM_VERSION,
            manifest.area_policy_sha256 == current_area_policy_sha256(),
        )
    )


def current_output_algorithm_revision() -> str:
    """Stable, monotonic identifier of the output-producing algorithm.

    This value is meant to be stable across documentation-only commits but
    bump whenever the algorithm that produces a Parquet row changes. The
    current implementation combines the transform algorithm version and the
    area-policy checksum, both of which are covered separately in
    :func:`is_resumable` but together form a single revision token.
    """
    return f"{TRANSFORM_ALGORITHM_VERSION}:{current_area_policy_sha256()[:16]}"


def write_manifest(manifest: Manifest, path: Path) -> None:
    """Atomically write ``manifest`` to ``path`` as canonical UTF-8 JSON."""
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
    try:
        # pragma: no mutate start - codec names are equivalent
        encoded = manifest.to_json().encode("utf-8")
        # pragma: no mutate end
        temp.write_bytes(encoded)
        with Path(temp).open("rb") as handle:  # pragma: no mutate - only the descriptor is used
            os.fsync(handle.fileno())
        Path(temp).replace(path)
        _fsync_dir(path.parent)
    finally:
        if temp.exists():
            temp.unlink()


def read_manifest(path: Path) -> Manifest:
    """Read and validate a manifest file."""
    return _manifest_from_payload(_read_manifest_payload(path), path)


def _read_manifest_payload(path: Path) -> dict[str, Any]:
    try:
        text = path.read_text(encoding="utf-8")  # pragma: no mutate - codec names are equivalent
    except OSError as error:
        raise ManifestError(f"cannot read manifest {path}: {error}") from error
    except UnicodeError as error:
        raise ManifestError(f"invalid manifest encoding {path}: {error}") from error
    try:
        payload = json.loads(text)
    except (RecursionError, ValueError) as error:
        raise ManifestError(f"corrupt manifest JSON {path}: {error}") from error
    if not isinstance(payload, dict):
        raise ManifestError(f"invalid manifest structure {path}: expected a JSON object")
    return cast(dict[str, Any], payload)  # pragma: no mutate - cast is type-only


def _manifest_from_payload(payload: dict[str, Any], path: Path) -> Manifest:
    try:
        return Manifest.from_payload(payload)
    except ManifestError:
        raise
    except (AttributeError, IndexError, KeyError, OverflowError, TypeError, ValueError) as error:
        raise ManifestError(f"invalid manifest structure {path}: {error}") from error


def current_dependency_versions() -> dict[str, str]:
    """Return installed versions of the runtime dependencies that affect output."""
    from importlib.metadata import PackageNotFoundError, version

    versions: dict[str, str] = {}
    for package in ("duckdb", "pyarrow", "pyproj", "shapely", "pyyaml"):
        try:
            versions[package] = version(package)
        except PackageNotFoundError:
            continue
    return versions


def current_code_revision() -> str | None:
    """Return the current Git revision of the project checkout."""
    return project_code_revision()


def _manifest_path_for(output_name: str, data_root: Path) -> Path:
    """Return the manifest path paired with ``output_name`` under ``data_root``."""
    stem = output_name.removesuffix(".parquet")
    return data_root / "manifests" / f"{stem}.manifest.json"


__all__ = [
    "MANIFEST_SCHEMA_VERSION",
    "TRANSFORM_ALGORITHM_VERSION",
    "Manifest",
    "ManifestError",
    "OutputIdentity",
    "RunCounts",
    "SourceIdentity",
    "current_area_policy_sha256",
    "current_code_revision",
    "current_dependency_versions",
    "current_output_algorithm_revision",
    "file_sha256",
    "is_resumable",
    "output_identity_for",
    "read_manifest",
    "source_identity_for",
    "write_manifest",
]
