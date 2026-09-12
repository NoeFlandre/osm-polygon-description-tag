"""Bounded, resumable, crash-safe processing of one source shard.

The worker streams projected Arrow batches from a single staged Parquet file,
detects the language of every description value it contains, and commits each
batch as an annotation part, a receipt, and a checkpoint, in that order. The
checkpoint is the single source of truth on resume: parts or receipts written
after the last committed checkpoint are deterministically rewritten rather than
adopted, so an interruption at any write boundary can neither lose annotations
nor duplicate them.
"""

import math
import time
from collections import OrderedDict
from collections.abc import Callable, Iterator
from dataclasses import dataclass
from pathlib import Path
from typing import Final

import pyarrow as pa
import pyarrow.parquet as pq

from osm_polygon_description_tag.dataset.languages.annotations import (
    AnnotationError,
    DescriptionAnnotation,
    TextAnalysis,
    annotation_table,
    read_annotation_part,
    validate_annotation_table_without_reserving,
    write_annotation_part,
)
from osm_polygon_description_tag.dataset.languages.checkpoint import (
    MAX_BATCH_SIZE,
    CheckpointError,
    PartReceipt,
    ShardCheckpoint,
    ShardPaths,
    ShardStatus,
    part_name_for_offset,
    read_checkpoint,
    read_receipt,
    shard_paths,
    validate_receipt_chain,
    write_checkpoint,
    write_receipt,
)
from osm_polygon_description_tag.dataset.languages.detector import LanguageDetectionCallable
from osm_polygon_description_tag.dataset.languages.records import (
    extract_description_entries,
)
from osm_polygon_description_tag.dataset.languages.snapshot import (
    SnapshotManifest,
    SourceFileSnapshot,
    verify_source_file,
)
from osm_polygon_description_tag.dataset.manifest import file_sha256
from osm_polygon_description_tag.dataset.sentences.splitter import (
    GatedSentenceSplitter,
    SentenceSplitter,
)

INPUT_COLUMNS: Final = ("source_pbf", "osm_type", "osm_id", "tags")
DEFAULT_BATCH_SIZE: Final = 512
DEFAULT_BUDGET_SECONDS: Final = 20 * 60.0
DEFAULT_CACHE_ENTRIES: Final = 4096

Clock = Callable[[], float]


def _validate_budget_seconds(seconds: object) -> float:
    if isinstance(seconds, bool) or not isinstance(seconds, int | float):
        raise TypeError("budget seconds must be a real number")
    numeric = float(seconds)
    if not math.isfinite(numeric):
        raise ValueError("budget seconds must be finite")
    if numeric <= 0:
        raise ValueError("budget seconds must be positive")
    return numeric


class ProcessingBudget:
    """A monotonic wall-clock budget checked only between whole batches."""

    __slots__ = ("_clock", "_seconds", "_started")

    def __init__(self, seconds: float = DEFAULT_BUDGET_SECONDS, *, clock: Clock | None = None):
        self._seconds = _validate_budget_seconds(seconds)
        self._clock = time.monotonic if clock is None else clock
        self._started: float | None = None

    def start(self) -> None:
        """Record the budget's start instant."""
        self._started = self._clock()

    @property
    def elapsed(self) -> float:
        """Return seconds elapsed since :meth:`start`."""
        if self._started is None:
            return 0.0
        return self._clock() - self._started

    def exhausted(self) -> bool:
        """Return whether the budget has been spent."""
        return self.elapsed >= self._seconds

    def expired(self) -> bool:
        """Return whether a just-finished operation exceeded the budget."""
        return self.elapsed >= self._seconds


class BoundedTextCache:
    """Reuse inference for identical text without growing without bound.

    Distinct objects sharing the same description text still receive their own
    annotation row; what is shared is the inference --- detection and splitting
    alike --- because both are pure functions of the text.
    """

    __slots__ = ("_entries", "_max_entries")

    def __init__(self, max_entries: int = DEFAULT_CACHE_ENTRIES) -> None:
        if type(max_entries) is not int or max_entries < 1:
            raise ValueError("cache max_entries must be a positive integer")
        self._max_entries = max_entries
        self._entries: OrderedDict[str, TextAnalysis] = OrderedDict()

    def __len__(self) -> int:
        return len(self._entries)

    def analysis_for(self, text: str, analyse: Callable[[str], TextAnalysis]) -> TextAnalysis:
        """Return the analysis of ``text``, reusing a cached one."""
        cached = self._entries.get(text)
        if cached is not None:
            self._entries.move_to_end(text)
            return cached
        result = analyse(text)
        self._entries[text] = result
        if len(self._entries) > self._max_entries:
            self._entries.popitem(last=False)  # pragma: no mutate - last=None is equivalent
        return result


@dataclass(frozen=True, slots=True)
class ShardOutcome:
    """The durable position of one shard after a bounded processing attempt."""

    shard: str
    status: ShardStatus
    input_cursor: int
    input_row_count: int
    annotation_count: int
    completed_parts: tuple[str, ...]
    resumed_from: int

    @property
    def is_complete(self) -> bool:
        """Return whether every input row of the shard has been processed."""
        return self.status is ShardStatus.COMPLETE


@dataclass(frozen=True, slots=True)
class _ResumeState:
    cursor: int
    annotation_count: int
    completed_parts: tuple[str, ...]
    annotation_identities: set[str]
    checkpoint: ShardCheckpoint | None


def _validate_batch_size(batch_size: int) -> int:
    if type(batch_size) is not int or batch_size < 1:
        raise ValueError("batch_size must be a positive integer")
    if batch_size > MAX_BATCH_SIZE:
        raise ValueError(f"batch_size must not exceed {MAX_BATCH_SIZE}")
    return batch_size


def _checkpoint_for(
    snapshot: SnapshotManifest,
    shard: str,
    batch_size: int,
    expected: SourceFileSnapshot,
    *,
    cursor: int,
    annotation_count: int,
    completed_parts: tuple[str, ...],
) -> ShardCheckpoint:
    status = ShardStatus.COMPLETE if cursor >= expected.row_count else ShardStatus.PAUSED
    return ShardCheckpoint(
        snapshot_id=snapshot.snapshot_id,
        model_config_fingerprint=snapshot.model_config_fingerprint,
        shard=shard,
        batch_size=batch_size,
        input_row_count=expected.row_count,
        input_cursor=cursor,
        annotation_count=annotation_count,
        completed_parts=completed_parts,
        status=status,
    )


def _validate_run_identity(
    checkpoint: ShardCheckpoint, snapshot: SnapshotManifest, shard: str
) -> None:
    if checkpoint.snapshot_id != snapshot.snapshot_id:
        raise CheckpointError("checkpoint belongs to a different input snapshot")
    if checkpoint.model_config_fingerprint != snapshot.model_config_fingerprint:
        raise CheckpointError("checkpoint belongs to a different detector configuration")
    if checkpoint.shard != shard:
        raise CheckpointError("checkpoint belongs to a different shard")


def _validate_checkpoint_identity(
    checkpoint: ShardCheckpoint,
    snapshot: SnapshotManifest,
    shard: str,
    batch_size: int,
    expected: SourceFileSnapshot,
) -> None:
    _validate_run_identity(checkpoint, snapshot, shard)
    if checkpoint.input_row_count != expected.row_count:
        raise CheckpointError("checkpoint input row count does not match the source file")
    # An initialized checkpoint with no committed part has not fixed a batch
    # grid yet, so any batch size may still be chosen for the first batch.
    if checkpoint.completed_parts and checkpoint.batch_size != batch_size:
        raise CheckpointError(
            f"checkpoint was written with batch size {checkpoint.batch_size}; "
            f"resume with that size or start a new run directory"
        )


def _verify_committed_part(
    paths: ShardPaths,
    part_name: str,
    checkpoint: ShardCheckpoint,
    seen_identities: set[str],
) -> PartReceipt:
    receipt_path = paths.receipt(part_name)
    part_path = paths.part(part_name)
    _require_regular(part_path, f"committed part is missing: {part_name}")
    _require_regular(receipt_path, f"committed receipt is missing: {part_name}")
    receipt = read_receipt(receipt_path)
    if receipt.snapshot_id != checkpoint.snapshot_id or receipt.shard != checkpoint.shard:
        raise CheckpointError(f"receipt does not belong to this run: {part_name}")
    if receipt.part_sha256 != file_sha256(part_path):
        raise CheckpointError(f"committed part does not match its receipt: {part_name}")
    _verify_part_is_readable(part_path, receipt, seen_identities)
    return receipt


def _require_regular(path: Path, message: str) -> None:
    if path.is_symlink() or not path.is_file():
        raise CheckpointError(message)


def _verify_part_is_readable(
    part_path: Path, receipt: PartReceipt, seen_identities: set[str]
) -> None:
    try:
        table = read_annotation_part(part_path)
        identities = validate_annotation_table_without_reserving(
            table,
            snapshot_id=receipt.snapshot_id,
            model_config_fingerprint=receipt.model_config_fingerprint,
            seen_identities=seen_identities,
        )
    except AnnotationError as error:
        raise CheckpointError(f"committed part is unreadable: {part_path.name}: {error}") from error
    if table.num_rows != receipt.row_count:
        raise CheckpointError(f"committed part row count does not match its receipt: {part_path}")
    seen_identities.update(identities)


def _resume_state(
    paths: ShardPaths,
    snapshot: SnapshotManifest,
    shard: str,
    batch_size: int,
    expected: SourceFileSnapshot,
) -> _ResumeState:
    if not paths.checkpoint.exists() and not paths.checkpoint.is_symlink():
        return _ResumeState(0, 0, (), set(), None)
    checkpoint = read_checkpoint(paths.checkpoint)
    _validate_checkpoint_identity(checkpoint, snapshot, shard, batch_size, expected)
    identities = _committed_annotation_identities(paths, checkpoint, expected)
    return _ResumeState(
        checkpoint.input_cursor,
        checkpoint.annotation_count,
        checkpoint.completed_parts,
        identities,
        checkpoint,
    )


def _committed_annotation_identities(
    paths: ShardPaths, checkpoint: ShardCheckpoint, expected: SourceFileSnapshot
) -> set[str]:
    identities: set[str] = set()
    receipts = tuple(
        _verify_committed_part(paths, part_name, checkpoint, identities)
        for part_name in checkpoint.completed_parts
    )
    validate_receipt_chain(
        checkpoint,
        iter(receipts),
        source_sha256=expected.sha256,
        source_schema_fingerprint=expected.schema_fingerprint,
    )
    verified = sum(receipt.row_count for receipt in receipts)
    if verified != checkpoint.annotation_count:
        raise CheckpointError("committed parts do not account for the recorded annotations")
    return identities


def _starting_row_groups(
    metadata: pq.FileMetaData, start_row: int, batch_size: int
) -> tuple[list[int], int]:
    """Choose row groups without changing the global batch grid.

    ``ParquetFile.iter_batches`` starts a new batch grid at the first selected
    row group. Keep the latest earlier row-group boundary aligned to
    ``batch_size`` so the selected stream has the same batch boundaries as a
    stream that started at row zero.
    """
    offset = 0
    # ``offset`` is zero on the first iteration and ``0 % batch_size`` is zero, so both
    # values below are reassigned before anything reads them; with no row groups the
    # loop never runs and the early ``return`` uses neither.
    aligned_index = 0  # pragma: no mutate - reassigned before its first read
    aligned_offset = 0  # pragma: no mutate - reassigned before its first read
    for index in range(metadata.num_row_groups):
        if offset % batch_size == 0:
            aligned_index = index
            aligned_offset = offset
        rows = metadata.row_group(index).num_rows
        if offset + rows > start_row:
            return list(range(aligned_index, metadata.num_row_groups)), aligned_offset
        offset += rows
    return [], offset


def _iter_input_batches(
    parquet: pq.ParquetFile,
    batch_size: int,
    start_row: int,
) -> Iterator[tuple[int, pa.RecordBatch]]:
    """Yield ``(input_row_offset, batch)`` from the first batch at ``start_row``.

    Metadata skips earlier row groups where their boundaries align with the
    batch grid. Any committed batches after that boundary are decoded and
    skipped without repeating inference.
    """
    row_groups, offset = _starting_row_groups(parquet.metadata, start_row, batch_size)
    if not row_groups:
        return
    batches = parquet.iter_batches(
        batch_size=batch_size,
        columns=list(INPUT_COLUMNS),
        row_groups=row_groups,
    )
    for batch in batches:
        if offset >= start_row:
            yield offset, batch
        offset += batch.num_rows


def _batch_annotations(
    batch: pa.RecordBatch,
    analyse: Callable[[str], TextAnalysis],
    cache: BoundedTextCache,
) -> list[DescriptionAnnotation]:
    annotations: list[DescriptionAnnotation] = []
    for row in batch.to_pylist():
        for entry in extract_description_entries(row):
            analysis = cache.analysis_for(entry.original_text, analyse)
            annotations.append(DescriptionAnnotation.from_analysis(entry, analysis))
    return annotations


def _text_analyser(
    detector: LanguageDetectionCallable, splitter: SentenceSplitter
) -> Callable[[str], TextAnalysis]:
    """Compose detection and gated splitting into one pass over a text."""
    gate = GatedSentenceSplitter(splitter)

    def analyse(text: str) -> TextAnalysis:
        detection = detector(text)
        return TextAnalysis(detection, gate.split_for(text, detection))

    return analyse


def _start_budget(budget: ProcessingBudget | None) -> None:
    if budget is not None:
        budget.start()


def _budget_is_exhausted(budget: ProcessingBudget | None) -> bool:
    return budget is not None and budget.exhausted()


def _batch_budget_allows_commit(budget: ProcessingBudget | None) -> bool:
    return budget is None or not budget.expired()


@dataclass(frozen=True, slots=True)
class _CommitContext:
    paths: ShardPaths
    snapshot: SnapshotManifest
    shard: str
    batch_size: int
    expected: SourceFileSnapshot


def _commit_batch(
    context: _CommitContext,
    annotations: list[DescriptionAnnotation],
    *,
    row_start: int,
    row_end: int,
    annotation_count: int,
    completed_parts: tuple[str, ...],
    seen_identities: set[str],
) -> tuple[int, tuple[str, ...]]:
    snapshot = context.snapshot
    part_name = part_name_for_offset(row_start)
    table = annotation_table(
        annotations,
        snapshot_id=snapshot.snapshot_id,
        model_config_fingerprint=snapshot.model_config_fingerprint,
    )
    identities = validate_annotation_table_without_reserving(
        table,
        snapshot_id=snapshot.snapshot_id,
        model_config_fingerprint=snapshot.model_config_fingerprint,
        seen_identities=seen_identities,
    )
    part_sha256 = write_annotation_part(context.paths.part(part_name), table)
    receipt = PartReceipt(
        snapshot_id=snapshot.snapshot_id,
        model_config_fingerprint=snapshot.model_config_fingerprint,
        shard=context.shard,
        input_row_start=row_start,
        input_row_end=row_end,
        part_name=part_name,
        part_sha256=part_sha256,
        row_count=table.num_rows,
        source_sha256=context.expected.sha256,
        source_schema_fingerprint=context.expected.schema_fingerprint,
    )
    write_receipt(context.paths.receipt(part_name), receipt)
    total = annotation_count + table.num_rows
    parts = (*completed_parts, part_name)
    write_checkpoint(
        context.paths.checkpoint,
        _checkpoint_for(
            snapshot,
            context.shard,
            context.batch_size,
            context.expected,
            cursor=row_end,
            annotation_count=total,
            completed_parts=parts,
        ),
    )
    seen_identities.update(identities)
    return total, parts


def _outcome(
    shard: str,
    checkpoint: ShardCheckpoint,
    resumed_from: int,
) -> ShardOutcome:
    return ShardOutcome(
        shard=shard,
        status=checkpoint.status,
        input_cursor=checkpoint.input_cursor,
        input_row_count=checkpoint.input_row_count,
        annotation_count=checkpoint.annotation_count,
        completed_parts=checkpoint.completed_parts,
        resumed_from=resumed_from,
    )


def process_shard(
    run_dir: Path,
    source_dir: Path,
    shard: str,
    *,
    detector: LanguageDetectionCallable,
    splitter: SentenceSplitter,
    snapshot: SnapshotManifest,
    batch_size: int = DEFAULT_BATCH_SIZE,
    budget: ProcessingBudget | None = None,
    cache_entries: int = DEFAULT_CACHE_ENTRIES,
) -> ShardOutcome:
    """Process one staged shard within a bounded budget, resuming if needed.

    Only the selected shard's staged source file must be present; the rest of
    the snapshot is used for identity, not for reading. Returns a paused
    outcome when the budget is exhausted, leaving a checkpoint that a later
    invocation resumes from exactly.
    """
    _validate_batch_size(batch_size)
    expected = snapshot.source_file(shard)
    source_path = verify_source_file(snapshot, source_dir, shard).relative_path
    paths = shard_paths(run_dir, shard)
    resume = _resume_state(paths, snapshot, shard, batch_size, expected)
    context = _CommitContext(paths, snapshot, shard, batch_size, expected)
    if resume.checkpoint is not None and resume.checkpoint.is_complete:
        return _outcome(shard, resume.checkpoint, resume.cursor)
    return _run_batches(
        context,
        analyse=_text_analyser(detector, splitter),
        source_path=source_dir / source_path,
        resume=resume,
        budget=budget,
        cache=BoundedTextCache(cache_entries),
    )


def _run_batches(
    context: _CommitContext,
    *,
    analyse: Callable[[str], TextAnalysis],
    source_path: Path,
    resume: _ResumeState,
    budget: ProcessingBudget | None,
    cache: BoundedTextCache,
) -> ShardOutcome:
    _start_budget(budget)
    cursor = resume.cursor
    annotation_count = resume.annotation_count
    completed_parts = resume.completed_parts
    seen_identities = resume.annotation_identities
    parquet = pq.ParquetFile(source_path)
    for row_start, batch in _iter_input_batches(parquet, context.batch_size, cursor):
        if _budget_is_exhausted(budget):
            break
        annotations = _batch_annotations(batch, analyse, cache)
        if not _batch_budget_allows_commit(budget):
            break
        annotation_count, completed_parts = _commit_batch(
            context,
            annotations,
            row_start=row_start,
            row_end=row_start + batch.num_rows,
            annotation_count=annotation_count,
            completed_parts=completed_parts,
            seen_identities=seen_identities,
        )
        cursor = row_start + batch.num_rows
    checkpoint = _checkpoint_for(
        context.snapshot,
        context.shard,
        context.batch_size,
        context.expected,
        cursor=cursor,
        annotation_count=annotation_count,
        completed_parts=completed_parts,
    )
    write_checkpoint(context.paths.checkpoint, checkpoint)
    return _outcome(context.shard, checkpoint, resume.cursor)


__all__ = [
    "DEFAULT_BATCH_SIZE",
    "DEFAULT_BUDGET_SECONDS",
    "DEFAULT_CACHE_ENTRIES",
    "INPUT_COLUMNS",
    "MAX_BATCH_SIZE",
    "BoundedTextCache",
    "ProcessingBudget",
    "ShardOutcome",
    "process_shard",
]
