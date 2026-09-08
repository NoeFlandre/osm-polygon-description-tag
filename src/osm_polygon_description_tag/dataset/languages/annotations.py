"""Explicit, stable annotation schema for one description per output row.

The counting unit is one *description value*, not one polygon: an object with a
base ``description`` and three localized ``description:*`` values contributes
four annotation rows. Every row carries its own entry identity, provenance, the
exact original text, the detection outcome, and the run identity that produced
it, so a part file is self-describing and independently verifiable.
"""

from collections.abc import Iterable, Mapping, Sequence
from pathlib import Path
from typing import Final, cast

import pyarrow as pa
import pyarrow.parquet as pq

from osm_polygon_description_tag.dataset.languages.atomic import atomic_write_via
from osm_polygon_description_tag.dataset.languages.models import LanguageResult, LanguageStatus
from osm_polygon_description_tag.dataset.languages.records import DescriptionEntry
from osm_polygon_description_tag.dataset.manifest import file_sha256

ANNOTATION_SCHEMA_VERSION: Final = 1
ANNOTATION_COMPRESSION: Final = "zstd"

ANNOTATION_SCHEMA: Final = pa.schema(
    [
        pa.field("description_identity", pa.string(), nullable=False),
        pa.field("source_pbf", pa.string(), nullable=False),
        pa.field("osm_type", pa.string(), nullable=False),
        pa.field("osm_id", pa.int64(), nullable=False),
        pa.field("tag_key", pa.string(), nullable=False),
        pa.field("original_text", pa.string(), nullable=False),
        pa.field("text_sha256", pa.string(), nullable=False),
        pa.field("language_code", pa.string(), nullable=True),
        pa.field("top_score", pa.float64(), nullable=True),
        pa.field("runner_up_score", pa.float64(), nullable=True),
        pa.field("margin", pa.float64(), nullable=True),
        pa.field("status", pa.string(), nullable=False),
        pa.field("reason", pa.string(), nullable=False),
        pa.field("snapshot_id", pa.string(), nullable=False),
        pa.field("model_config_fingerprint", pa.string(), nullable=False),
    ]
)

_DICTIONARY_COLUMNS: Final = ("osm_type", "tag_key", "language_code", "status", "reason")


class AnnotationError(ValueError):
    """Raised when an annotation part is malformed or does not match its run."""


def annotation_row(
    entry: DescriptionEntry,
    result: LanguageResult,
    *,
    snapshot_id: str,
    model_config_fingerprint: str,
) -> dict[str, object]:
    """Build one annotation row from an entry and its detection result."""
    return {
        "description_identity": entry.description_identity,
        "source_pbf": entry.source_pbf,
        "osm_type": entry.osm_type,
        "osm_id": entry.osm_id,
        "tag_key": entry.tag_key,
        "original_text": entry.original_text,
        "text_sha256": entry.text_sha256,
        "language_code": result.language_code,
        "top_score": result.top_score,
        "runner_up_score": result.runner_up_score,
        "margin": result.margin,
        "status": str(result.status),
        "reason": result.reason,
        "snapshot_id": snapshot_id,
        "model_config_fingerprint": model_config_fingerprint,
    }


def annotation_table(
    pairs: Iterable[tuple[DescriptionEntry, LanguageResult]],
    *,
    snapshot_id: str,
    model_config_fingerprint: str,
) -> pa.Table:
    """Build one Arrow table holding exactly one row per description entry."""
    rows = [
        annotation_row(
            entry,
            result,
            snapshot_id=snapshot_id,
            model_config_fingerprint=model_config_fingerprint,
        )
        for entry, result in pairs
    ]
    table = pa.Table.from_pylist(rows, schema=ANNOTATION_SCHEMA)
    validate_annotation_table(
        table,
        snapshot_id=snapshot_id,
        model_config_fingerprint=model_config_fingerprint,
    )
    return table


def write_annotation_part(path: Path, table: pa.Table) -> str:
    """Atomically write one annotation part and return its SHA-256."""
    if table.schema != ANNOTATION_SCHEMA:
        raise AnnotationError("annotation table does not match the annotation schema")
    if table.num_rows:
        first = table.slice(0, 1).to_pylist()[0]
        validate_annotation_table(
            table,
            snapshot_id=first["snapshot_id"],
            model_config_fingerprint=first["model_config_fingerprint"],
        )
    atomic_write_via(path, lambda temp: _write_parquet(temp, table))
    return file_sha256(path)


def _write_parquet(path: Path, table: pa.Table) -> None:
    with pq.ParquetWriter(
        path,
        ANNOTATION_SCHEMA,
        compression=ANNOTATION_COMPRESSION,
        use_dictionary=_DICTIONARY_COLUMNS,
    ) as writer:
        writer.write_table(table)


def read_annotation_part(path: Path) -> pa.Table:
    """Read one annotation part, rejecting unreadable or off-schema files."""
    try:
        table = pq.read_table(path)
    except (OSError, pa.ArrowException) as error:
        raise AnnotationError(f"cannot read annotation part {path}: {error}") from error
    validate_annotation_schema(table.schema, path)
    return table


def validate_annotation_schema(schema: pa.Schema, path: Path) -> None:
    """Reject any annotation schema that is not exactly the frozen contract."""
    if tuple(schema.names) != tuple(ANNOTATION_SCHEMA.names):
        raise AnnotationError(f"annotation part has unexpected columns: {path}")
    for name in ANNOTATION_SCHEMA.names:
        expected = ANNOTATION_SCHEMA.field(name)
        actual = schema.field(name)
        if actual.type != expected.type or actual.nullable != expected.nullable:
            raise AnnotationError(f"annotation part field mismatch for {name}: {path}")


def validate_annotation_table(
    table: pa.Table,
    *,
    snapshot_id: str,
    model_config_fingerprint: str,
    seen_identities: set[str] | None = None,
    merge_seen: bool = True,
) -> tuple[str, ...]:
    """Validate row semantics, run binding, and identity uniqueness.

    Identity membership is checked against ``seen_identities`` without copying
    its history. When ``merge_seen`` is true, the caller's set is updated only
    after the complete table validates; callers that need a later count or
    commit check can merge the returned identities themselves.

    The local work is bounded by this annotation part. The caller's identity
    set remains proportional to the validation scope: one shard for the
    worker, or all selected shards for run validation.
    """
    validate_annotation_schema(table.schema, Path("<in-memory annotation table>"))
    local_identities: set[str] = set()
    identities: list[str] = []
    for index, row in enumerate(table.to_pylist()):
        identity = _validate_annotation_row(
            row,
            index,
            snapshot_id=snapshot_id,
            model_config_fingerprint=model_config_fingerprint,
        )
        _validate_new_identity(identity, index, local_identities, seen_identities)
        local_identities.add(identity)
        identities.append(identity)
    if seen_identities is not None and merge_seen:
        seen_identities.update(identities)
    return tuple(identities)


def _validate_new_identity(
    identity: str,
    index: int,
    local_identities: set[str],
    seen_identities: set[str] | None,
) -> None:
    if identity in local_identities or (
        seen_identities is not None and identity in seen_identities
    ):
        raise AnnotationError(f"duplicate description identity at row {index}: {identity}")


def _validate_annotation_row(
    row: Mapping[str, object],
    index: int,
    *,
    snapshot_id: str,
    model_config_fingerprint: str,
) -> str:
    if row.get("snapshot_id") != snapshot_id:
        raise AnnotationError(f"annotation row {index} has a different snapshot id")
    if row.get("model_config_fingerprint") != model_config_fingerprint:
        raise AnnotationError(f"annotation row {index} has a different model fingerprint")
    entry = _entry_from_annotation_row(row, index)
    if row.get("text_sha256") != entry.text_sha256:
        raise AnnotationError(f"annotation row {index} has an invalid text_sha256")
    if row.get("description_identity") != entry.description_identity:
        raise AnnotationError(f"annotation row {index} has an invalid description_identity")
    _result_from_annotation_row(row, index)
    return entry.description_identity


def _entry_from_annotation_row(row: Mapping[str, object], index: int) -> DescriptionEntry:
    try:
        return DescriptionEntry(
            cast(str, row["source_pbf"]),
            cast(str, row["osm_type"]),
            cast(int, row["osm_id"]),
            cast(str, row["tag_key"]),
            cast(str, row["original_text"]),
        )
    except (TypeError, ValueError) as error:
        raise AnnotationError(
            f"annotation row {index} has invalid entry fields: {error}"
        ) from error


def _result_from_annotation_row(row: Mapping[str, object], index: int) -> LanguageResult:
    try:
        return LanguageResult(
            cast(str | None, row["language_code"]),
            cast(int | float | None, row["top_score"]),
            cast(int | float | None, row["runner_up_score"]),
            cast(int | float | None, row["margin"]),
            cast(LanguageStatus, row["status"]),
            cast(str, row["reason"]),
        )
    except (TypeError, ValueError) as error:
        raise AnnotationError(
            f"annotation row {index} has invalid detection result: {error}"
        ) from error


def annotation_identities(table: pa.Table) -> Sequence[str]:
    """Return the description identities recorded in one annotation table."""
    return table.column("description_identity").to_pylist()


__all__ = [
    "ANNOTATION_COMPRESSION",
    "ANNOTATION_SCHEMA",
    "ANNOTATION_SCHEMA_VERSION",
    "AnnotationError",
    "annotation_identities",
    "annotation_row",
    "annotation_table",
    "read_annotation_part",
    "validate_annotation_schema",
    "validate_annotation_table",
    "write_annotation_part",
]
