"""Persisted deduplication state and the identity of staged inputs.

The state file records which finalized Parquets a staged deduplication pass
read and what it produced. These helpers read and atomically write that file,
hash the inputs, and name any input that drifted from the recorded identity.
"""

from __future__ import annotations

import json
from collections.abc import Iterable, Mapping
from pathlib import Path
from typing import Any, cast

from osm_polygon_description_tag.dataset.manifest import file_sha256
from osm_polygon_description_tag.runtime.atomic import atomic_write_text


class DeduplicationError(RuntimeError):
    """Raised when finalized artifacts cannot be deduplicated safely."""


def read_state(path: Path) -> dict[str, Any] | None:
    if not path.is_file():
        return None
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as error:
        raise DeduplicationError(f"invalid deduplication state: {path}") from error
    if not isinstance(value, dict):
        raise DeduplicationError(f"deduplication state must be an object: {path}")
    return cast(dict[str, Any], value)


def write_state(path: Path, payload: Mapping[str, object]) -> None:
    body = json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n"
    atomic_write_text(path, body)


def input_hashes(parquets: Iterable[Path]) -> dict[str, str]:
    return {path.name: file_sha256(path) for path in parquets}


def staged_output_hashes(state: Mapping[str, Any]) -> dict[str, str]:
    entries = cast(list[Mapping[str, Any]], state["files"])
    return {Path(str(entry["parquet"])).name: str(entry["parquet_sha256"]) for entry in entries}


def recorded_input_hashes(state: Mapping[str, Any]) -> Mapping[str, Any]:
    expected = state.get("inputs")
    if not isinstance(expected, Mapping):
        raise DeduplicationError("staged deduplication state is missing input identities")
    return expected


def staged_input_drift_names(
    current: Mapping[str, str],
    expected: Mapping[str, Any],
    staged_outputs: Mapping[str, str],
) -> tuple[str, ...]:
    names = set(current) | set(expected)
    return tuple(
        sorted(
            name for name in names if input_name_drifted(name, current, expected, staged_outputs)
        )
    )


def input_name_drifted(
    name: str,
    current: Mapping[str, str],
    expected: Mapping[str, Any],
    staged_outputs: Mapping[str, str],
) -> bool:
    if name not in current or name not in expected:
        return True
    return current[name] != expected[name] and current[name] != staged_outputs.get(name)


__all__ = [
    "DeduplicationError",
    "input_hashes",
    "read_state",
    "recorded_input_hashes",
    "staged_input_drift_names",
    "staged_output_hashes",
    "write_state",
]
