"""Non-destructive Hugging Face publication planning and execution gate.

The allowlist explicitly enumerates the four artifact categories uploaded to
the public dataset repository. Symlinks, temporary files, unknown top-level
paths, and missing card or statistics are rejected at plan time. Execution
requires the caller to confirm the exact plan identity and re-verifies every
file checksum before invoking the ``hf`` CLI.
"""

import json
import subprocess
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

from osm_polygon_description_tag.manifest import file_sha256

REPO_ID = "NoeFlandre/osm-polygon-description-tag"
_ALLOWED_TOP_LEVEL = {"README.md", "stats.json", "data", "manifests", "publication-plans"}
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

Runner = Callable[[list[str]], None]


class PublicationError(ValueError):
    """Raised for publication planning or execution failures."""


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


def _collect_allowlisted_files(data_root: Path) -> tuple[UploadItem, ...]:
    # Enumerate top-level structure: only the allowlisted entries are permitted.
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
    """Build the allowlisted, identity-hashed upload plan for ``data_root``."""
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
    """Re-verify every file identity matches the plan before upload."""
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


def execute_upload(
    plan: UploadPlan,
    *,
    confirmation: str,
    runner: Runner | None = None,
) -> None:
    """Execute the upload only after the exact plan identity is confirmed."""
    if confirmation != plan.identity_sha256:
        raise PublicationError("confirmation does not match plan identity (refusing to upload)")
    _verify_identity(plan)
    command = _build_command(plan)
    if runner is None:
        subprocess.run(  # noqa: S603 - controlled argument array, no shell
            command,
            check=True,
            shell=False,
        )
    else:
        runner(command)
