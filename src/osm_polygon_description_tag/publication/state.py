"""Atomic resumable publication state."""

from __future__ import annotations

import json
import os
import uuid
from pathlib import Path

from osm_polygon_description_tag.publication.models import UploadPlan

PUBLICATION_STATE_FILENAME = "publication-state.json"


class PublicationStateError(RuntimeError):
    """Raised when publication state is malformed or unsupported."""


def _atomic_write_json(path: Path, payload: dict[str, object]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    body = json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n"
    temp = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
    try:
        temp.write_text(body, encoding="utf-8")
        with open(temp, "rb") as handle:
            os.fsync(handle.fileno())
        directory_fd = os.open(str(path.parent), os.O_RDONLY)
        try:
            os.fsync(directory_fd)
        finally:
            os.close(directory_fd)
        os.replace(temp, path)
    finally:
        if temp.exists():
            temp.unlink()


def read_publication_state(data_root: Path) -> dict[str, object]:
    state_path = data_root / PUBLICATION_STATE_FILENAME
    if not state_path.is_file():
        return {"schema_version": 1, "published": {}}
    return cast_dict(json.loads(state_path.read_text(encoding="utf-8")))


def _write_publication_state(
    data_root: Path,
    *,
    source_name: str,
    source_sha256: str,
    output_sha256: str,
    output_bytes: int,
    remote_revision: str,
    artifact_identity: str,
    completed_at: str,
) -> dict[str, object]:
    state = read_publication_state(data_root)
    if state.get("schema_version") != 1:
        raise PublicationStateError(
            f"unsupported publication state schema: {state.get('schema_version')!r}"
        )
    published = cast_dict(state.setdefault("published", {}))
    published[source_name] = {
        "source_sha256": source_sha256,
        "output_sha256": output_sha256,
        "output_bytes": output_bytes,
        "remote_revision": remote_revision,
        "artifact_identity": artifact_identity,
        "completed_at": completed_at,
    }
    state["last_updated_at"] = completed_at
    _atomic_write_json(data_root / PUBLICATION_STATE_FILENAME, state)
    return state


def _metadata_state_matches(data_root: Path, metadata_plan: UploadPlan) -> bool:
    """True only when the recorded metadata state matches the current plan."""
    state = read_publication_state(data_root)
    metadata = state.get("metadata")
    if not isinstance(metadata, dict):
        return False
    if metadata.get("identity_sha256") != metadata_plan.identity_sha256:
        return False
    readme_path = data_root / "README.md"
    stats_path = data_root / "stats.json"
    if not readme_path.is_file() or not stats_path.is_file():
        return False
    from osm_polygon_description_tag.dataset.manifest import file_sha256

    expected_readme_sha = file_sha256(readme_path)
    expected_stats_sha = file_sha256(stats_path)
    if metadata.get("readme_sha256") != expected_readme_sha:
        return False
    if metadata.get("stats_sha256") != expected_stats_sha:
        return False
    return (
        metadata.get("readme_size_bytes") == readme_path.stat().st_size
        and metadata.get("stats_size_bytes") == stats_path.stat().st_size
    )


def _write_metadata_state(
    data_root: Path,
    *,
    identity_sha256: str,
    readme_sha256: str,
    stats_sha256: str,
    readme_size_bytes: int,
    stats_size_bytes: int,
    verified_revision: str,
    completed_at: str,
) -> dict[str, object]:
    state = read_publication_state(data_root)
    if state.get("schema_version") != 1:
        raise PublicationStateError(
            f"unsupported publication state schema: {state.get('schema_version')!r}"
        )
    state["metadata"] = {
        "identity_sha256": identity_sha256,
        "readme_sha256": readme_sha256,
        "stats_sha256": stats_sha256,
        "readme_size_bytes": readme_size_bytes,
        "stats_size_bytes": stats_size_bytes,
        "verified_revision": verified_revision,
        "completed_at": completed_at,
    }
    state["last_updated_at"] = completed_at
    _atomic_write_json(data_root / PUBLICATION_STATE_FILENAME, state)
    return state


def cast_dict(value: object) -> dict[str, object]:
    if not isinstance(value, dict):
        raise PublicationStateError(f"expected dict, got {type(value).__name__}")
    return value
