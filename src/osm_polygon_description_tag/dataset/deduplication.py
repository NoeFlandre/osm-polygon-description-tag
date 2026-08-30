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
    Manifest,
    _manifest_path_for,
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


@dataclass(frozen=True)
class _DeduplicationContext:
    data_root: Path
    state_path: Path
    state: Mapping[str, Any] | None
    parquets: tuple[Path, ...]
    manifests: dict[str, Manifest]
    inputs: dict[str, str]
    input_rows: int


def _version(value: object) -> int:
    return int(value) if isinstance(value, int | float) else -1


def _parse_timestamp(value: object) -> datetime | None:
    if isinstance(value, datetime):
        return value
    if not isinstance(value, str) or not value.strip():
        return None
    try:
        # Keep explicit UTC-suffix normalization for Python versions whose
        # ``fromisoformat`` implementation does not accept ``Z`` directly.
        # pragma: no mutate start
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
        # pragma: no mutate end
    except ValueError:
        return None


def _timestamp_rank(value: object) -> float:
    parsed = _parse_timestamp(value)
    if parsed is None:
        return 0.0
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    # ``timestamp`` preserves the instant for either aware timezone choice.
    # pragma: no mutate start
    return parsed.astimezone(UTC).timestamp()
    # pragma: no mutate end


def _row_fingerprint(row: Mapping[str, object]) -> str:
    payload = json.dumps(
        {key: row.get(key) for key in SCHEMA.names if key != "source_pbf"},
        # Explicitly keep Unicode in the byte-stable fingerprint payload.
        ensure_ascii=False,
        sort_keys=True,
        default=str,
        separators=(",", ":"),
    )
    # UTF-8 codec names are case-insensitive.
    # pragma: no mutate start
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()
    # pragma: no mutate end


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
    return cast(dict[str, Any], value)  # pragma: no mutate - runtime-only type narrowing


def _write_state(path: Path, payload: Mapping[str, object]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
    try:
        temp.write_text(
            # Explicitly keep Unicode in the byte-stable state payload.
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


def _promote_artifact(
    stage_dir: Path,
    data_root: Path,
    relative: str,
    expected_sha: str,
) -> bool:
    staged = stage_dir / relative
    target = data_root / relative
    if staged.is_file() and not staged.is_symlink():
        target.parent.mkdir(parents=True, exist_ok=True)
        os.replace(staged, target)
        return True
    if target.is_file() and file_sha256(target) == expected_sha:
        return False
    raise DeduplicationError(f"missing staged artifact: {staged}")


def _promote_entry(
    stage_dir: Path,
    data_root: Path,
    entry: Mapping[str, Any],
    *,
    promotion_hook: Callable[[int], None] | None,
    promoted: int,
) -> int:
    artifacts = (
        (str(entry["parquet"]), str(entry["parquet_sha256"])),
        (str(entry["manifest"]), str(entry["manifest_sha256"])),
    )
    for relative, expected_sha in artifacts:
        if _promote_artifact(stage_dir, data_root, relative, expected_sha):
            promoted += 1
            if promotion_hook is not None:
                promotion_hook(promoted)
    return promoted


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
    for entry in cast(
        list[dict[str, Any]], state["files"]
    ):  # pragma: no mutate - runtime-only type narrowing
        promoted = _promote_entry(
            stage_dir,
            data_root,
            entry,
            promotion_hook=promotion_hook,
            promoted=promoted,
        )
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


def _skipped_result(
    input_rows: int = 0,
    output_rows: int = 0,
    duplicate_rows: int = 0,
) -> DeduplicationResult:
    return DeduplicationResult("skipped", input_rows, output_rows, duplicate_rows, 0)


def _validated_parquets(data_root: Path) -> tuple[Path, ...]:
    data_dir = data_root / "data"
    if not data_dir.is_dir() or not tuple(data_dir.glob("*.parquet")):
        return ()
    validated = validate_finalized_artifacts(data_root)
    # pragma: no mutate start - runtime-only type narrowing
    parquets = tuple(cast(Sequence[Path], validated["parquets"]))
    # pragma: no mutate end
    for parquet in parquets:
        validate_geoparquet(parquet)
    return parquets


def _complete_result(
    state: Mapping[str, Any] | None,
    inputs: Mapping[str, str],
    parquets: Sequence[Path],
) -> DeduplicationResult | None:
    if state is None or not _complete_state_matches(state, inputs):
        return None
    output_rows = _current_output_rows(parquets)
    return _skipped_result(
        int(state.get("input_rows", output_rows)),
        output_rows,
        int(state.get("duplicate_rows", 0)),
    )


def _read_manifests(data_root: Path, parquets: Sequence[Path]) -> dict[str, Manifest]:
    return {
        parquet.name: read_manifest(_manifest_path_for(parquet.name, data_root))
        for parquet in parquets
    }


def _prepare_context(
    data_root: Path,
    state_path: Path,
    state: Mapping[str, Any] | None,
) -> tuple[_DeduplicationContext | None, DeduplicationResult | None]:
    parquets = _validated_parquets(data_root)
    if not parquets:
        return None, _skipped_result()
    inputs = _input_hashes(parquets)
    result = _complete_result(state, inputs, parquets)
    if result is not None:
        return None, result
    manifests = _read_manifests(data_root, parquets)
    return (
        _DeduplicationContext(
            data_root=data_root,
            state_path=state_path,
            state=state,
            parquets=parquets,
            manifests=manifests,
            inputs=inputs,
            input_rows=_current_output_rows(parquets),
        ),
        None,
    )


def _assert_known_sources(
    connection: duckdb.DuckDBPyConnection,
    manifests: Mapping[str, Manifest],
) -> None:
    source_names = {
        str(value[0])
        for value in connection.execute("SELECT DISTINCT source_pbf FROM deduplicated").fetchall()
    }
    manifest_source_names = {manifest.source.name for manifest in manifests.values()}
    unknown_sources = source_names - manifest_source_names
    if unknown_sources:
        raise DeduplicationError(f"rows reference unknown source PBFs: {sorted(unknown_sources)}")


def _stage_source(
    connection: duckdb.DuckDBPyConnection,
    parquet: Path,
    manifest: Manifest,
    stage_root: Path,
) -> tuple[int, dict[str, Any] | None]:
    old_rows = int(pq.ParquetFile(parquet).metadata.num_rows)
    # pragma: no mutate start
    count_row = connection.execute(
        "SELECT COUNT(*) FROM deduplicated WHERE source_pbf = ?",
        [manifest.source.name],
    ).fetchone()
    # pragma: no mutate end
    new_rows = int(count_row[0] if count_row else 0)
    dropped = old_rows - new_rows
    if dropped <= 0:
        return new_rows, None
    staged_parquet = stage_root / "data" / parquet.name
    staged_manifest = _manifest_path_for(parquet.name, stage_root)
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
    return new_rows, {
        "parquet": f"data/{parquet.name}",
        "manifest": f"manifests/{staged_manifest.name}",
        "parquet_sha256": file_sha256(staged_parquet),
        "manifest_sha256": file_sha256(staged_manifest),
        "duplicate_rows": dropped,
    }


def _stage_changes(
    context: _DeduplicationContext,
    stage_root: Path,
) -> tuple[list[dict[str, Any]], int]:
    changed: list[dict[str, Any]] = []
    output_rows = 0
    connection = duckdb.connect()
    try:
        _canonical_relation(connection, context.parquets)
        _assert_known_sources(connection, context.manifests)
        for parquet in context.parquets:
            new_rows, record = _stage_source(
                connection,
                parquet,
                context.manifests[parquet.name],
                stage_root,
            )
            output_rows += new_rows
            if record is not None:
                changed.append(record)
    finally:
        connection.close()
    return changed, output_rows


def _state_payload(
    context: _DeduplicationContext,
    changed: list[dict[str, Any]],
    output_rows: int,
) -> dict[str, object]:
    return {
        "schema_version": 1,
        "policy_version": DEDUPLICATION_POLICY_VERSION,
        "policy_sha256": DEDUPLICATION_POLICY_SHA256,
        "status": "staged" if changed else "complete",
        "inputs": dict(sorted(context.inputs.items())),
        "outputs": dict(sorted(context.inputs.items())) if not changed else {},
        "input_rows": context.input_rows,
        "output_rows": output_rows,
        "duplicate_rows": context.input_rows - output_rows,
        "files": changed,
    }


def _finish_deduplication(
    context: _DeduplicationContext,
    stage_dir: Path,
    state: dict[str, object],
    changed: list[dict[str, Any]],
    output_rows: int,
    promotion_hook: Callable[[int], None] | None,
) -> DeduplicationResult:
    if changed:
        state["stage_dir"] = stage_dir.as_posix()
        _write_state(context.state_path, state)
        return _resume_staged(
            context.data_root,
            context.state_path,
            state,
            promotion_hook=promotion_hook,
        )
    state["outputs"] = dict(sorted(context.inputs.items()))
    _write_state(context.state_path, state)
    return _skipped_result(context.input_rows, output_rows)


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
    context, result = _prepare_context(data_root, state_path, state)
    if result is not None:
        return result
    if context is None:
        raise DeduplicationError("deduplication context was not created")
    stage_token = uuid.uuid4().hex
    stage_dir = Path(".work") / "dedup" / stage_token
    stage_root = data_root / stage_dir
    changed, output_rows = _stage_changes(context, stage_root)
    state_payload = _state_payload(context, changed, output_rows)
    return _finish_deduplication(
        context,
        stage_dir,
        state_payload,
        changed,
        output_rows,
        promotion_hook,
    )


__all__ = [
    "DEDUPLICATION_POLICY_SHA256",
    "DEDUPLICATION_POLICY_VERSION",
    "DUPLICATE_REJECTION_REASON",
    "DeduplicationError",
    "DeduplicationResult",
    "deduplicate_dataset",
    "select_canonical_row",
]
