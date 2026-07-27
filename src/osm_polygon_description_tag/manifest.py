"""Versioned manifests, artifact identity, and resumption decisions.

A manifest is canonical UTF-8 JSON recording source/output identity, tool and
library versions, timing, and factual feature/rejection counts. Resumption
trusts an output only when its manifest still matches the current source and
output identities.
"""

import hashlib
import json
import os
import subprocess
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any, cast

MANIFEST_SCHEMA_VERSION = 1
_SHA256_CHUNK = 8 * 1024 * 1024


class ManifestError(ValueError):
    """Raised for unreadable, corrupt, or unsupported manifests."""


def file_sha256(path: Path) -> str:
    """Return the hex SHA-256 of ``path`` without mutating it."""
    digest = hashlib.sha256()
    with open(path, "rb") as handle:
        for chunk in iter(lambda: handle.read(_SHA256_CHUNK), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _fsync_dir(directory: Path) -> None:
    fd = os.open(str(directory), os.O_RDONLY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


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


@dataclass(frozen=True)
class Manifest:
    manifest_schema_version: int
    schema_version: int
    geoparquet_version: str
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
        if version != MANIFEST_SCHEMA_VERSION:
            raise ManifestError(f"unsupported manifest schema version: {version!r}")
        source_raw = cast(dict[str, Any], payload["source"])
        output_raw = cast(dict[str, Any], payload["output"])
        counts_raw = cast(dict[str, Any], payload["counts"])
        return cls(
            manifest_schema_version=int(payload["manifest_schema_version"]),
            schema_version=int(payload["schema_version"]),
            geoparquet_version=str(payload["geoparquet_version"]),
            source=SourceIdentity(
                name=str(source_raw["name"]),
                size_bytes=int(source_raw["size_bytes"]),
                mtime_ns=int(source_raw["mtime_ns"]),
                sha256=str(source_raw["sha256"]),
            ),
            output=OutputIdentity(
                name=str(output_raw["name"]),
                size_bytes=int(output_raw["size_bytes"]),
                sha256=str(output_raw["sha256"]),
            ),
            osmium_version=cast("str | None", payload.get("osmium_version")),
            dependency_versions=dict(cast(dict[str, str], payload["dependency_versions"])),
            code_revision=cast("str | None", payload.get("code_revision")),
            started_at=str(payload["started_at"]),
            completed_at=str(payload["completed_at"]),
            counts=RunCounts(
                emitted_features=int(counts_raw["emitted_features"]),
                included_rows=int(counts_raw["included_rows"]),
                rejections=dict(cast(dict[str, int], counts_raw["rejections"])),
            ),
        )


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
    """True only when the manifest version, source, and output identities all agree."""
    if manifest.manifest_schema_version != MANIFEST_SCHEMA_VERSION:
        return False
    return manifest.source == source_identity and manifest.output == output_identity


def write_manifest(manifest: Manifest, path: Path) -> None:
    """Atomically write ``manifest`` to ``path`` as canonical UTF-8 JSON."""
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
    try:
        temp.write_text(manifest.to_json(), encoding="utf-8")
        with open(temp, "rb") as handle:
            os.fsync(handle.fileno())
        _fsync_dir(path.parent)
        os.replace(temp, path)
    finally:
        if temp.exists():
            temp.unlink()


def read_manifest(path: Path) -> Manifest:
    """Read and validate a manifest file."""
    try:
        text = path.read_text(encoding="utf-8")
    except OSError as error:
        raise ManifestError(f"cannot read manifest {path}: {error}") from error
    try:
        payload = cast(dict[str, Any], json.loads(text))
    except json.JSONDecodeError as error:
        raise ManifestError(f"corrupt manifest JSON {path}: {error}") from error
    return Manifest.from_payload(payload)


def current_dependency_versions() -> dict[str, str]:
    """Return installed versions of the runtime dependencies that affect output."""
    from importlib.metadata import PackageNotFoundError, version

    versions: dict[str, str] = {}
    for package in ("pyarrow", "pyproj", "shapely", "pyyaml"):
        try:
            versions[package] = version(package)
        except PackageNotFoundError:
            continue
    return versions


def current_code_revision(cwd: Path | None = None) -> str | None:
    """Return the current Git revision, or ``None`` if unavailable."""
    try:
        completed = subprocess.run(
            ["git", "rev-parse", "HEAD"],  # noqa: S607 - read-only git query, controlled args
            cwd=str(cwd) if cwd else None,
            capture_output=True,
            text=True,
            timeout=5,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    if completed.returncode != 0:
        return None
    revision = completed.stdout.strip()
    return revision or None
