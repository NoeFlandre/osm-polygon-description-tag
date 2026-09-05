"""Atomic resumable publication state."""

from __future__ import annotations

import json
import os
import uuid
from pathlib import Path
from typing import cast

from osm_polygon_description_tag.dataset.manifest import file_sha256
from osm_polygon_description_tag.publication.artifacts import (
    AREA_HISTOGRAM_ARTIFACT,
    DATASET_CARD_HERO_ARTIFACT,
    H3_MAP_ARTIFACT,
    METADATA_ARTIFACTS,
    metadata_paths,
)
from osm_polygon_description_tag.publication.models import UploadPlan

PUBLICATION_STATE_FILENAME = "publication-state.json"
H3_MAP_ASSET_RELATIVE_PATH = H3_MAP_ARTIFACT.relative_path
AREA_HISTOGRAM_ASSET_RELATIVE_PATH = AREA_HISTOGRAM_ARTIFACT.relative_path
DATASET_CARD_HERO_ASSET_RELATIVE_PATH = DATASET_CARD_HERO_ARTIFACT.relative_path
_H3_MAP_SHA256_FIELD = H3_MAP_ARTIFACT.sha256_field
_H3_MAP_SIZE_FIELD = H3_MAP_ARTIFACT.size_field
_AREA_HISTOGRAM_SHA256_FIELD = AREA_HISTOGRAM_ARTIFACT.sha256_field
_AREA_HISTOGRAM_SIZE_FIELD = AREA_HISTOGRAM_ARTIFACT.size_field
_DATASET_CARD_HERO_SHA256_FIELD = DATASET_CARD_HERO_ARTIFACT.sha256_field
_DATASET_CARD_HERO_SIZE_FIELD = DATASET_CARD_HERO_ARTIFACT.size_field


class PublicationStateError(RuntimeError):
    """Raised when publication state is malformed or unsupported."""


def _atomic_write_json(path: Path, payload: dict[str, object]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    # pragma: no mutate start - None and False are equivalent for json.ensure_ascii
    body = json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n"
    # pragma: no mutate end
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
    # pragma: no mutate start - publication state is always UTF-8 JSON
    state_text = state_path.read_text(encoding="utf-8")
    # pragma: no mutate end
    return cast_dict(json.loads(state_text))


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
    state = _current_state(data_root)
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
    paths = _metadata_paths(data_root)
    if not _metadata_files_exist(paths):
        return False
    typed_metadata = cast(dict[str, object], metadata)  # pragma: no mutate - static narrowing only
    return _metadata_identity_matches(typed_metadata, paths)


def _metadata_paths(data_root: Path) -> dict[str, Path]:
    return metadata_paths(data_root)


def _metadata_files_exist(paths: dict[str, Path]) -> bool:
    return all(path.is_file() and not path.is_symlink() for path in paths.values())


def _metadata_identity_matches(metadata: dict[str, object], paths: dict[str, Path]) -> bool:
    return all(
        metadata.get(artifact.sha256_field) == file_sha256(paths[artifact.key])
        and metadata.get(artifact.size_field) == paths[artifact.key].stat().st_size
        for artifact in METADATA_ARTIFACTS
    )


def _write_metadata_state(
    data_root: Path,
    *,
    identity_sha256: str,
    readme_sha256: str,
    stats_sha256: str,
    readme_size_bytes: int,
    stats_size_bytes: int,
    h3_map_sha256: str | None = None,
    h3_map_size_bytes: int | None = None,
    area_histogram_sha256: str | None = None,
    area_histogram_size_bytes: int | None = None,
    dataset_card_hero_sha256: str | None = None,
    dataset_card_hero_size_bytes: int | None = None,
    verified_revision: str,
    completed_at: str,
) -> dict[str, object]:
    state = _current_state(data_root)
    payload: dict[str, object] = {
        "identity_sha256": identity_sha256,
        "readme_sha256": readme_sha256,
        "stats_sha256": stats_sha256,
        "readme_size_bytes": readme_size_bytes,
        "stats_size_bytes": stats_size_bytes,
        "verified_revision": verified_revision,
        "completed_at": completed_at,
    }
    _add_optional_metadata_fields(
        payload,
        (
            (_H3_MAP_SHA256_FIELD, h3_map_sha256),
            (_H3_MAP_SIZE_FIELD, h3_map_size_bytes),
            (_AREA_HISTOGRAM_SHA256_FIELD, area_histogram_sha256),
            (_AREA_HISTOGRAM_SIZE_FIELD, area_histogram_size_bytes),
            (_DATASET_CARD_HERO_SHA256_FIELD, dataset_card_hero_sha256),
            (_DATASET_CARD_HERO_SIZE_FIELD, dataset_card_hero_size_bytes),
        ),
    )
    state["metadata"] = payload
    state["last_updated_at"] = completed_at
    _atomic_write_json(data_root / PUBLICATION_STATE_FILENAME, state)
    return state


def _current_state(data_root: Path) -> dict[str, object]:
    state = read_publication_state(data_root)
    if state.get("schema_version") != 1:
        raise PublicationStateError(
            f"unsupported publication state schema: {state.get('schema_version')!r}"
        )
    return state


def _add_optional_metadata_fields(
    payload: dict[str, object], fields: tuple[tuple[str, object | None], ...]
) -> None:
    for key, value in fields:
        if value is not None:
            payload[key] = value


def cast_dict(value: object) -> dict[str, object]:
    if not isinstance(value, dict):
        raise PublicationStateError(f"expected dict, got {type(value).__name__}")
    # pragma: no mutate start - cast is static-only; the runtime value is unchanged
    return cast(dict[str, object], value)
    # pragma: no mutate end
