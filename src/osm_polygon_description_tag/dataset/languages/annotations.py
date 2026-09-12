"""Explicit, stable annotation schema for one description per output row.

The counting unit is one *description value*, not one polygon: an object with a
base ``description`` and three localized ``description:*`` values contributes
four annotation rows. Every row carries its own entry identity, provenance, the
exact original text, the detection outcome, and the run identity that produced
it, so a part file is self-describing and independently verifiable.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Final, cast

import pyarrow as pa
import pyarrow.parquet as pq

from osm_polygon_description_tag.dataset.languages.atomic import atomic_write_via
from osm_polygon_description_tag.dataset.languages.models import LanguageResult, LanguageStatus
from osm_polygon_description_tag.dataset.languages.records import DescriptionEntry
from osm_polygon_description_tag.dataset.manifest import file_sha256
from osm_polygon_description_tag.dataset.sentences.models import (
    SentenceSplitResult,
    SentenceSplitStatus,
)

ANNOTATION_SCHEMA_VERSION: Final = 2
ANNOTATION_COMPRESSION: Final = "zstd"
_SPLIT_STATUS_VALUES: Final = frozenset(str(status) for status in SentenceSplitStatus)

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
        pa.field("split_status", pa.string(), nullable=False),
        pa.field("split_reason", pa.string(), nullable=False),
        pa.field("sentence_count", pa.int32(), nullable=False),
        pa.field("sentences", pa.list_(pa.string()), nullable=False),
    ]
)

_DICTIONARY_COLUMNS: Final = (
    "osm_type",
    "tag_key",
    "language_code",
    "status",
    "reason",
    "split_status",
    "split_reason",
)


class AnnotationError(ValueError):
    """Raised when an annotation part is malformed or does not match its run."""


@dataclass(frozen=True, slots=True)
class TextAnalysis:
    """What the pipeline computed about one description's text.

    Two objects sharing the same text share this, because both stages are pure
    functions of the text; the entry that carries it is not part of it.
    """

    language: LanguageResult
    split: SentenceSplitResult


@dataclass(frozen=True, slots=True)
class DescriptionAnnotation:
    """Everything one description carries into its published row.

    Detection and splitting each produce their own result, and both travel with
    the entry they describe. Bundling them keeps the row builder's signature
    stable as the pipeline grows a stage, instead of widening a tuple at every
    call site between here and the worker.
    """

    entry: DescriptionEntry
    language: LanguageResult
    split: SentenceSplitResult

    @classmethod
    def from_analysis(
        cls, entry: DescriptionEntry, analysis: TextAnalysis
    ) -> DescriptionAnnotation:
        """Attach one entry to the analysis of the text it carries."""
        return cls(entry, analysis.language, analysis.split)


def annotation_row(
    annotation: DescriptionAnnotation,
    *,
    snapshot_id: str,
    model_config_fingerprint: str,
) -> dict[str, object]:
    """Build one annotation row from one description's detection and splitting."""
    entry = annotation.entry
    result = annotation.language
    split = annotation.split
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
        "split_status": str(split.status),
        "split_reason": split.reason,
        "sentence_count": len(split.sentences),
        "sentences": list(split.sentences),
    }


def annotation_table(
    annotations: Iterable[DescriptionAnnotation],
    *,
    snapshot_id: str,
    model_config_fingerprint: str,
) -> pa.Table:
    """Build one Arrow table holding exactly one row per description entry."""
    rows = [
        annotation_row(
            annotation,
            snapshot_id=snapshot_id,
            model_config_fingerprint=model_config_fingerprint,
        )
        for annotation in annotations
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
        validate_annotation_table(
            table,
            snapshot_id=table.column("snapshot_id")[0].as_py(),
            model_config_fingerprint=table.column("model_config_fingerprint")[0].as_py(),
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
    after the complete table validates. Callers that must reserve later --- once
    their own commit or count has succeeded --- use
    :func:`validate_annotation_table_without_reserving` instead.

    The local work is bounded by this annotation part. The caller's identity
    set remains proportional to the validation scope: one shard for the
    worker, or all selected shards for run validation.
    """
    identities = _validated_identities(
        table,
        snapshot_id=snapshot_id,
        model_config_fingerprint=model_config_fingerprint,
        seen_identities=seen_identities,
    )
    if seen_identities is not None and merge_seen:
        seen_identities.update(identities)
    return identities


def validate_annotation_table_without_reserving(
    table: pa.Table,
    *,
    snapshot_id: str,
    model_config_fingerprint: str,
    seen_identities: set[str] | None = None,
) -> tuple[str, ...]:
    """Validate one annotation table without reserving its identities.

    The caller reserves the returned identities itself, after its own commit or
    count has succeeded, so a part that is written but not committed --- or one
    that is refused for its row count --- leaves the caller's set untouched.
    """
    return _validated_identities(
        table,
        snapshot_id=snapshot_id,
        model_config_fingerprint=model_config_fingerprint,
        seen_identities=seen_identities,
    )


def _validated_identities(
    table: pa.Table,
    *,
    snapshot_id: str,
    model_config_fingerprint: str,
    seen_identities: set[str] | None,
) -> tuple[str, ...]:
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
    _validate_split_columns(row, index)
    return entry.description_identity


def _validate_split_columns(row: Mapping[str, object], index: int) -> None:
    """Reject a row whose splitting outcome contradicts its own sentences."""
    sentences = _row_sentences(row, index)
    _require_matching_sentence_count(row, index, sentences)
    _require_supported_split_status(row, index, sentences)
    _require_split_reason(row, index)


def _row_sentences(row: Mapping[str, object], index: int) -> list[str]:
    sentences = row.get("sentences")
    if not isinstance(sentences, list):
        raise AnnotationError(f"annotation row {index} has invalid sentences")
    typed = cast(list[object], sentences)  # pragma: no mutate - static narrowing
    if any(not isinstance(sentence, str) for sentence in typed):
        raise AnnotationError(f"annotation row {index} has invalid sentences")
    return cast(list[str], typed)  # pragma: no mutate - static narrowing


def _require_matching_sentence_count(
    row: Mapping[str, object], index: int, sentences: list[str]
) -> None:
    """The count is redundant on purpose, so a damaged part cannot pass silently."""
    if row.get("sentence_count") != len(sentences):
        raise AnnotationError(f"annotation row {index} has a wrong sentence_count")


def _require_supported_split_status(
    row: Mapping[str, object], index: int, sentences: list[str]
) -> None:
    """Only a split row may carry sentences, and only a known status may appear."""
    status = row.get("split_status")
    if status not in _SPLIT_STATUS_VALUES:
        raise AnnotationError(f"annotation row {index} has an unsupported split_status")
    if sentences and status != str(SentenceSplitStatus.SPLIT):
        raise AnnotationError(f"annotation row {index} has sentences without a split status")


def _require_split_reason(row: Mapping[str, object], index: int) -> None:
    """Every row says why it carries the sentences it does, or why it carries none."""
    reason = row.get("split_reason")
    if not isinstance(reason, str) or not reason:
        raise AnnotationError(f"annotation row {index} has an empty split_reason")


def _entry_from_annotation_row(row: Mapping[str, object], index: int) -> DescriptionEntry:
    try:
        source_pbf = cast(str, row["source_pbf"])  # pragma: no mutate - static cast
        osm_type = cast(str, row["osm_type"])  # pragma: no mutate - static cast
        osm_id = cast(int, row["osm_id"])  # pragma: no mutate - static cast
        tag_key = cast(str, row["tag_key"])  # pragma: no mutate - static cast
        original_text = cast(str, row["original_text"])  # pragma: no mutate - static cast
        return DescriptionEntry(source_pbf, osm_type, osm_id, tag_key, original_text)
    except (TypeError, ValueError) as error:
        raise AnnotationError(
            f"annotation row {index} has invalid entry fields: {error}"
        ) from error


def _result_from_annotation_row(row: Mapping[str, object], index: int) -> LanguageResult:
    try:
        language_code = cast(str | None, row["language_code"])  # pragma: no mutate - static cast
        top_score = cast(int | float | None, row["top_score"])  # pragma: no mutate - static cast
        runner_up_score = cast(
            int | float | None, row["runner_up_score"]
        )  # pragma: no mutate - static cast
        margin = cast(int | float | None, row["margin"])  # pragma: no mutate - static cast
        status = cast(LanguageStatus, row["status"])  # pragma: no mutate - static cast
        reason = cast(str, row["reason"])  # pragma: no mutate - static cast
        return LanguageResult(language_code, top_score, runner_up_score, margin, status, reason)
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
    "DescriptionAnnotation",
    "TextAnalysis",
    "annotation_identities",
    "annotation_row",
    "annotation_table",
    "read_annotation_part",
    "validate_annotation_schema",
    "validate_annotation_table",
    "validate_annotation_table_without_reserving",
    "write_annotation_part",
]
