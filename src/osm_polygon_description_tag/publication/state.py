"""Atomic resumable publication state."""

from __future__ import annotations

import json
import os
import uuid
from pathlib import Path
from typing import cast

from osm_polygon_description_tag.publication.models import UploadPlan
from osm_polygon_description_tag.publication.planning import (
    AREA_HISTOGRAM_ASSET_RELATIVE,
    DATASET_CARD_HERO_ASSET_RELATIVE,
    H3_MAP_ASSET_RELATIVE,
)

PUBLICATION_STATE_FILENAME = "publication-state.json"
H3_MAP_ASSET_RELATIVE_PATH = H3_MAP_ASSET_RELATIVE
AREA_HISTOGRAM_ASSET_RELATIVE_PATH = AREA_HISTOGRAM_ASSET_RELATIVE
DATASET_CARD_HERO_ASSET_RELATIVE_PATH = DATASET_CARD_HERO_ASSET_RELATIVE
_H3_MAP_SHA256_FIELD = "h3_map_sha256"
_H3_MAP_SIZE_FIELD = "h3_map_size_bytes"
_AREA_HISTOGRAM_SHA256_FIELD = "area_histogram_sha256"
_AREA_HISTOGRAM_SIZE_FIELD = "area_histogram_size_bytes"
_DATASET_CARD_HERO_SHA256_FIELD = "dataset_card_hero_sha256"
_DATASET_CARD_HERO_SIZE_FIELD = "dataset_card_hero_size_bytes"


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
    map_path = data_root / H3_MAP_ASSET_RELATIVE_PATH
    histogram_path = data_root / AREA_HISTOGRAM_ASSET_RELATIVE_PATH
    hero_path = data_root / DATASET_CARD_HERO_ASSET_RELATIVE_PATH
    if (
        not readme_path.is_file()
        or not stats_path.is_file()
        or not map_path.is_file()
        or not histogram_path.is_file()
        or not hero_path.is_file()
    ):
        return False
    from osm_polygon_description_tag.dataset.manifest import file_sha256

    expected_readme_sha = file_sha256(readme_path)
    expected_stats_sha = file_sha256(stats_path)
    expected_map_sha = file_sha256(map_path)
    expected_histogram_sha = file_sha256(histogram_path)
    expected_hero_sha = file_sha256(hero_path)
    if metadata.get("readme_sha256") != expected_readme_sha:
        return False
    if metadata.get("stats_sha256") != expected_stats_sha:
        return False
    if metadata.get(_H3_MAP_SHA256_FIELD) != expected_map_sha:
        return False
    if metadata.get(_AREA_HISTOGRAM_SHA256_FIELD) != expected_histogram_sha:
        return False
    if metadata.get(_DATASET_CARD_HERO_SHA256_FIELD) != expected_hero_sha:
        return False
    return (
        metadata.get("readme_size_bytes") == readme_path.stat().st_size
        and metadata.get("stats_size_bytes") == stats_path.stat().st_size
        and metadata.get(_H3_MAP_SIZE_FIELD) == map_path.stat().st_size
        and metadata.get(_AREA_HISTOGRAM_SIZE_FIELD) == histogram_path.stat().st_size
        and metadata.get(_DATASET_CARD_HERO_SIZE_FIELD) == hero_path.stat().st_size
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
    state = read_publication_state(data_root)
    if state.get("schema_version") != 1:
        raise PublicationStateError(
            f"unsupported publication state schema: {state.get('schema_version')!r}"
        )
    payload: dict[str, object] = {
        "identity_sha256": identity_sha256,
        "readme_sha256": readme_sha256,
        "stats_sha256": stats_sha256,
        "readme_size_bytes": readme_size_bytes,
        "stats_size_bytes": stats_size_bytes,
        "verified_revision": verified_revision,
        "completed_at": completed_at,
    }
    if h3_map_sha256 is not None:
        payload[_H3_MAP_SHA256_FIELD] = h3_map_sha256
    if h3_map_size_bytes is not None:
        payload[_H3_MAP_SIZE_FIELD] = h3_map_size_bytes
    if area_histogram_sha256 is not None:
        payload[_AREA_HISTOGRAM_SHA256_FIELD] = area_histogram_sha256
    if area_histogram_size_bytes is not None:
        payload[_AREA_HISTOGRAM_SIZE_FIELD] = area_histogram_size_bytes
    if dataset_card_hero_sha256 is not None:
        payload[_DATASET_CARD_HERO_SHA256_FIELD] = dataset_card_hero_sha256
    if dataset_card_hero_size_bytes is not None:
        payload[_DATASET_CARD_HERO_SIZE_FIELD] = dataset_card_hero_size_bytes
    state["metadata"] = payload
    state["last_updated_at"] = completed_at
    _atomic_write_json(data_root / PUBLICATION_STATE_FILENAME, state)
    return state


def cast_dict(value: object) -> dict[str, object]:
    if not isinstance(value, dict):
        raise PublicationStateError(f"expected dict, got {type(value).__name__}")
    return cast(dict[str, object], value)
