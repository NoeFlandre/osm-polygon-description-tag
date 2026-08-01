"""Deterministic global OSM-identity deduplication.

Per-PBF extracts overlap at regional boundaries.  This module performs one
global pass over validated GeoParquets, keeps one canonical row for each
``(osm_type, osm_id)``, and atomically promotes only the affected files.
"""

# The SQL below interpolates only frozen schema names and path literals escaped
# by ``_sql_literal``; row values remain parameterized.
# ruff: noqa: S608

from __future__ import annotations

import hashlib
import json
import os
import shutil
import uuid
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass, replace
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, cast

import duckdb
import pyarrow.parquet as pq

from osm_polygon_description_tag.dataset.manifest import (
    file_sha256,
    output_identity_for,
    read_manifest,
    write_manifest,
)
from osm_polygon_description_tag.dataset.schema import SCHEMA
from osm_polygon_description_tag.dataset.storage import (
    validate_finalized_artifacts,
    validate_geoparquet,
    write_geoparquet,
)

DEDUPLICATION_POLICY_VERSION = 1
DUPLICATE_REJECTION_REASON = "duplicate_osm_object"
_POLICY_TEXT = (
    "key=(osm_type,osm_id);winner=max(version);then=max(timestamp);"
    "then=min(source_pbf);then=min(row_fingerprint)"
)
DEDUPLICATION_POLICY_SHA256 = hashlib.sha256(_POLICY_TEXT.encode("utf-8")).hexdigest()
_STATE_RELATIVE_PATH = Path(".work") / "dedup-state.json"
_BATCH_SIZE = 4096


class DeduplicationError(RuntimeError):
    """Raised when finalized artifacts cannot be deduplicated safely."""


@dataclass(frozen=True)
class DeduplicationResult:
    """Machine-readable result of one global deduplication pass."""

    status: str
    input_rows: int
    output_rows: int
    duplicate_rows: int
    files_changed: int


def _version(value: object) -> int:
    return int(value) if isinstance(value, int | float) else -1


def _timestamp_rank(value: object) -> float:
    if isinstance(value, datetime):
        parsed = value
    elif isinstance(value, str) and value.strip():
        try:
            parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError:
            return 0.0
    else:
        return 0.0
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    return parsed.astimezone(UTC).timestamp()


def _row_fingerprint(row: Mapping[str, object]) -> str:
    payload = json.dumps(
        {key: row.get(key) for key in SCHEMA.names if key != "source_pbf"},
        ensure_ascii=False,
        sort_keys=True,
        default=str,
        separators=(",", ":"),
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def select_canonical_row(rows: Sequence[Mapping[str, object]]) -> Mapping[str, object]:
    """Select the stable canonical row for one OSM identity group."""
    if not rows:
        raise ValueError("cannot select a canonical row from an empty group")
    return min(
        rows,
        key=lambda row: (
            -_version(row.get("version")),
            -_timestamp_rank(row.get("timestamp")),
            str(row.get("source_pbf", "")),
            _row_fingerprint(row),
        ),
    )


def _sql_literal(value: str) -> str:
    return "'" + value.replace("'", "''") + "'"


def _read_state(path: Path) -> dict[str, Any] | None:
    if not path.is_file():
        return None
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as error:
        raise DeduplicationError(f"invalid deduplication state: {path}") from error
    if not isinstance(value, dict):
        raise DeduplicationError(f"deduplication state must be an object: {path}")
    return cast(dict[str, Any], value)


def _write_state(path: Path, payload: Mapping[str, object]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
    try:
        temp.write_text(
            json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )
        with open(temp, "rb") as handle:
            os.fsync(handle.fileno())
        os.replace(temp, path)
        directory_fd = os.open(str(path.parent), os.O_RDONLY)
        try:
            os.fsync(directory_fd)
        finally:
            os.close(directory_fd)
    finally:
        if temp.exists():
            temp.unlink()


def _input_hashes(parquets: Iterable[Path]) -> dict[str, str]:
    return {path.name: file_sha256(path) for path in parquets}


def _current_output_rows(parquets: Iterable[Path]) -> int:
    return sum(int(pq.ParquetFile(path).metadata.num_rows) for path in parquets)


def _complete_state_matches(state: Mapping[str, Any], current: Mapping[str, str]) -> bool:
    return (
        state.get("status") == "complete"
        and state.get("policy_sha256") == DEDUPLICATION_POLICY_SHA256
        and state.get("outputs") == dict(sorted(current.items()))
    )


def _promote_staged(
    data_root: Path,
    state: Mapping[str, Any],
    *,
    promotion_hook: Callable[[int], None] | None = None,
) -> None:
    stage_dir = data_root / str(state["stage_dir"])
    if not stage_dir.is_dir() or stage_dir.is_symlink():
        raise DeduplicationError(f"staged deduplication directory is missing: {stage_dir}")
    promoted = 0
    for entry in cast(list[dict[str, Any]], state["files"]):
        for relative, expected_sha in (
            (entry["parquet"], entry["parquet_sha256"]),
            (entry["manifest"], entry["manifest_sha256"]),
        ):
            staged = stage_dir / relative
            target = data_root / relative
            if not staged.is_file() or staged.is_symlink():
                if target.is_file() and file_sha256(target) == expected_sha:
                    continue
                raise DeduplicationError(f"missing staged artifact: {staged}")
            target.parent.mkdir(parents=True, exist_ok=True)
            os.replace(staged, target)
            promoted += 1
            if promotion_hook is not None:
                promotion_hook(promoted)
    shutil.rmtree(stage_dir)


def _resume_staged(
    data_root: Path,
    state_path: Path,
    state: Mapping[str, Any],
    *,
    promotion_hook: Callable[[int], None] | None = None,
) -> DeduplicationResult:
    _promote_staged(data_root, state, promotion_hook=promotion_hook)
    complete = dict(state)
    complete["status"] = "complete"
    complete.pop("stage_dir", None)
    complete["outputs"] = dict(
        sorted(_input_hashes(sorted((data_root / "data").glob("*.parquet"))).items())
    )
    _write_state(state_path, complete)
    return DeduplicationResult(
        status="deduplicated",
        input_rows=int(state["input_rows"]),
        output_rows=int(state["output_rows"]),
        duplicate_rows=int(state["duplicate_rows"]),
        files_changed=len(cast(list[dict[str, Any]], state["files"])),
    )


def _canonical_relation(connection: duckdb.DuckDBPyConnection, parquets: Sequence[Path]) -> None:
    paths = ", ".join(_sql_literal(str(path)) for path in parquets)
    columns = ", ".join(SCHEMA.names)
    connection.execute(
        f"""
        CREATE TEMP TABLE deduplicated AS
        SELECT {columns}
        FROM (
            SELECT *, ROW_NUMBER() OVER (
                PARTITION BY osm_type, osm_id
                ORDER BY version DESC NULLS LAST,
                         timestamp DESC NULLS LAST,
                         source_pbf ASC,
                         md5(concat_ws('|',
                             coalesce(cast(version AS VARCHAR), ''),
                             coalesce(cast(timestamp AS VARCHAR), ''),
                             source_pbf,
                             coalesce(description, ''),
                             hex(ST_AsWKB(geometry))
                         )) ASC
            ) AS _dedup_rank
            FROM read_parquet([{paths}])
        ) ranked
        WHERE _dedup_rank = 1
        """
    )


def _rows_for_source(
    connection: duckdb.DuckDBPyConnection, source_name: str
) -> Iterable[dict[str, object]]:
    query = (
        f"SELECT {', '.join(SCHEMA.names)} FROM deduplicated "
        "WHERE source_pbf = ? ORDER BY osm_type, osm_id"
    )
    reader = connection.execute(query, [source_name]).to_arrow_reader(_BATCH_SIZE)
    for batch in reader:
        yield from batch.to_pylist()


def deduplicate_dataset(
    data_root: Path,
    *,
    promotion_hook: Callable[[int], None] | None = None,
) -> DeduplicationResult:
    """Deduplicate all finalized per-PBF Parquets with atomic resumption."""
    state_path = data_root / _STATE_RELATIVE_PATH
    state = _read_state(state_path)
    if state is not None and state.get("status") == "staged":
        return _resume_staged(data_root, state_path, state, promotion_hook=promotion_hook)
    data_dir = data_root / "data"
    if not data_dir.is_dir() or not tuple(data_dir.glob("*.parquet")):
        return DeduplicationResult("skipped", 0, 0, 0, 0)
    validated = validate_finalized_artifacts(data_root)
    parquets = tuple(cast(Sequence[Path], validated["parquets"]))
    if not parquets:
        return DeduplicationResult("skipped", 0, 0, 0, 0)
    for parquet in parquets:
        validate_geoparquet(parquet)
    inputs = _input_hashes(parquets)
    if state is not None and _complete_state_matches(state, inputs):
        output_rows = _current_output_rows(parquets)
        return DeduplicationResult(
            "skipped",
            int(state.get("input_rows", output_rows)),
            output_rows,
            int(state.get("duplicate_rows", 0)),
            0,
        )

    manifests = {
        parquet.name: read_manifest(data_root / "manifests" / f"{parquet.stem}.manifest.json")
        for parquet in parquets
    }
    input_rows = _current_output_rows(parquets)
    stage_token = uuid.uuid4().hex
    stage_dir = Path(".work") / "dedup" / stage_token
    stage_root = data_root / stage_dir
    changed: list[dict[str, Any]] = []
    output_rows = 0
    connection = duckdb.connect()
    try:
        _canonical_relation(connection, parquets)
        source_names = {
            str(value[0])
            for value in connection.execute(
                "SELECT DISTINCT source_pbf FROM deduplicated"
            ).fetchall()
        }
        manifest_source_names = {manifest.source.name for manifest in manifests.values()}
        unknown_sources = source_names - manifest_source_names
        if unknown_sources:
            raise DeduplicationError(
                f"rows reference unknown source PBFs: {sorted(unknown_sources)}"
            )
        for parquet in parquets:
            manifest = manifests[parquet.name]
            old_rows = int(pq.ParquetFile(parquet).metadata.num_rows)
            count_row = connection.execute(
                "SELECT COUNT(*) FROM deduplicated WHERE source_pbf = ?",
                [manifest.source.name],
            ).fetchone()
            new_rows = int(count_row[0] if count_row else 0)
            output_rows += new_rows
            dropped = old_rows - new_rows
            if dropped <= 0:
                continue
            staged_parquet = stage_root / "data" / parquet.name
            staged_manifest = stage_root / "manifests" / f"{parquet.stem}.manifest.json"
            write_geoparquet(
                _rows_for_source(connection, manifest.source.name),
                staged_parquet,
                batch_size=_BATCH_SIZE,
            )
            rejection_counts = dict(manifest.counts.rejections)
            rejection_counts[DUPLICATE_REJECTION_REASON] = (
                rejection_counts.get(DUPLICATE_REJECTION_REASON, 0) + dropped
            )
            rewritten = replace(
                manifest,
                output=output_identity_for(staged_parquet),
                counts=replace(
                    manifest.counts,
                    included_rows=new_rows,
                    rejections=dict(sorted(rejection_counts.items())),
                ),
            )
            write_manifest(rewritten, staged_manifest)
            changed.append(
                {
                    "parquet": f"data/{parquet.name}",
                    "manifest": f"manifests/{staged_manifest.name}",
                    "parquet_sha256": file_sha256(staged_parquet),
                    "manifest_sha256": file_sha256(staged_manifest),
                    "duplicate_rows": dropped,
                }
            )
    finally:
        connection.close()

    state_payload: dict[str, object] = {
        "schema_version": 1,
        "policy_version": DEDUPLICATION_POLICY_VERSION,
        "policy_sha256": DEDUPLICATION_POLICY_SHA256,
        "status": "staged" if changed else "complete",
        "inputs": dict(sorted(inputs.items())),
        "outputs": dict(sorted(inputs.items())) if not changed else {},
        "input_rows": input_rows,
        "output_rows": output_rows,
        "duplicate_rows": input_rows - output_rows,
        "files": changed,
    }
    if changed:
        state_payload["stage_dir"] = stage_dir.as_posix()
        _write_state(state_path, state_payload)
        return _resume_staged(
            data_root,
            state_path,
            state_payload,
            promotion_hook=promotion_hook,
        )
    state_payload["outputs"] = dict(sorted(inputs.items()))
    _write_state(state_path, state_payload)
    return DeduplicationResult("skipped", input_rows, output_rows, 0, 0)


__all__ = [
    "DEDUPLICATION_POLICY_SHA256",
    "DEDUPLICATION_POLICY_VERSION",
    "DUPLICATE_REJECTION_REASON",
    "DeduplicationError",
    "DeduplicationResult",
    "deduplicate_dataset",
    "select_canonical_row",
]
