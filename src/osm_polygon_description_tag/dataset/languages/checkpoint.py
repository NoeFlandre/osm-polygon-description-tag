"""Durable local shard checkpoints, receipts, and the worker process lock.

A checkpoint is only ever written at a commit point, so any checkpoint found on
disk describes a consistent resumable position: either the input is fully
consumed (``complete``) or a worker should continue from ``input_cursor``
(``paused``). Receipts bind each committed part to the exact input rows that
produced it and to the run's immutable identity.
"""

import errno
import fcntl
import hashlib
import json
import re
from collections.abc import Iterator, Mapping
from contextlib import contextmanager
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path
from typing import Final

from osm_polygon_description_tag.dataset.languages.atomic import atomic_write_json
from osm_polygon_description_tag.dataset.languages.paths import relative_posix_path
from osm_polygon_description_tag.dataset.languages.payloads import PayloadReader, require_object

CHECKPOINT_SCHEMA_VERSION: Final = 1
RECEIPT_SCHEMA_VERSION: Final = 1
ANNOTATION_SCHEMA_VERSION: Final = 1
MAX_BATCH_SIZE: Final = 4096
WORKER_LOCK_FILENAME: Final = ".worker.lock"
_FINGERPRINT_PATTERN = re.compile(r"[0-9a-f]{64}\Z")
_PART_WIDTH: Final = 20
_PART_PATTERN = re.compile(rf"part-(?P<offset>[0-9]{{{_PART_WIDTH}}})\.parquet\Z")
_RECEIPT_PATTERN = re.compile(rf"part-[0-9]{{{_PART_WIDTH}}}\.json\Z")


class CheckpointError(ValueError):
    """Raised for corrupt, stale, or unsafe local worker state."""


class WorkerBusyError(CheckpointError):
    """Raised when another local worker owns the run lock."""


class ShardStatus(StrEnum):
    """Whether a shard still has input rows left to process."""

    PAUSED = "paused"
    COMPLETE = "complete"


def _validate_fingerprint(value: object, label: str) -> None:
    if not isinstance(value, str) or _FINGERPRINT_PATTERN.fullmatch(value) is None:
        raise CheckpointError(f"{label} must be a lowercase SHA-256 fingerprint")


def _validate_batch_size(value: object) -> None:
    if type(value) is not int or value <= 0:
        raise CheckpointError("checkpoint batch_size must be positive")
    if value > MAX_BATCH_SIZE:
        raise CheckpointError(f"checkpoint batch_size must not exceed {MAX_BATCH_SIZE}")


def _validate_cursor(value: object, label: str) -> None:
    if type(value) is not int or value < 0:
        raise CheckpointError(f"{label} must be a non-negative integer")


def _validate_relative_shard(value: object) -> None:
    text = relative_posix_path(value, error=CheckpointError, label="shard")
    if Path(text).suffix != ".parquet":
        raise CheckpointError("shard must be a Parquet path")


def _part_offset(value: object) -> int:
    """Validate one part name and return the input-row offset it encodes."""
    match = _PART_PATTERN.fullmatch(value) if isinstance(value, str) else None
    if match is None:
        raise CheckpointError("part name must be a relative part name")
    return int(match.group("offset"))


def part_name_for_offset(input_row_offset: int) -> str:
    """Return the deterministic part filename for a batch's input offset."""
    _validate_cursor(input_row_offset, "input_row_offset")
    if input_row_offset >= 10**_PART_WIDTH:
        raise CheckpointError("input row offset is too large for a part name")
    return f"part-{input_row_offset:0{_PART_WIDTH}d}.parquet"


def is_generated_part_name(name: str) -> bool:
    """Return whether ``name`` is a part filename this worker generates."""
    return _PART_PATTERN.fullmatch(name) is not None


def is_generated_receipt_name(name: str) -> bool:
    """Return whether ``name`` is a receipt filename this worker generates."""
    return _RECEIPT_PATTERN.fullmatch(name) is not None


def receipt_name_for_part(part_name: str) -> str:
    """Return the receipt filename that accompanies one committed part."""
    _part_offset(part_name)
    return f"{Path(part_name).stem}.json"


@dataclass(frozen=True, slots=True)
class ShardPaths:
    """Owned filesystem locations for one source shard."""

    root: Path
    parts: Path
    receipts: Path
    checkpoint: Path

    def part(self, part_name: str) -> Path:
        """Return the owned path of one committed part."""
        _part_offset(part_name)
        return self.parts / part_name

    def receipt(self, part_name: str) -> Path:
        """Return the owned path of one part's receipt."""
        return self.receipts / receipt_name_for_part(part_name)


def _shard_key(shard: str) -> str:
    _validate_relative_shard(shard)
    shard_bytes = shard.encode("utf-8")  # pragma: no mutate - codec alias only
    return hashlib.sha256(shard_bytes).hexdigest()[:32]


def shards_root(run_dir: Path) -> Path:
    """Return the single directory that owns every shard's state."""
    return run_dir / "shards"


def shard_paths(run_dir: Path, shard: str) -> ShardPaths:
    """Return deterministic owned paths without creating any filesystem state."""
    root = shards_root(run_dir) / _shard_key(shard)
    return ShardPaths(root, root / "parts", root / "receipts", root / "checkpoint.json")


@dataclass(frozen=True, slots=True)
class PartReceipt:
    """Receipt binding one committed part to a source cursor and run identity."""

    snapshot_id: str
    model_config_fingerprint: str
    shard: str
    input_row_start: int
    input_row_end: int
    part_name: str
    part_sha256: str
    row_count: int
    source_sha256: str
    source_schema_fingerprint: str
    annotation_schema_version: int = ANNOTATION_SCHEMA_VERSION

    def __post_init__(self) -> None:
        self._validate_identity()
        self._validate_cursor_binding()
        _validate_fingerprint(self.part_sha256, "part sha256")
        _validate_cursor(self.row_count, "receipt row_count")
        if self.annotation_schema_version != ANNOTATION_SCHEMA_VERSION:
            raise CheckpointError("unsupported annotation schema version")

    def _validate_identity(self) -> None:
        _validate_fingerprint(self.snapshot_id, "snapshot id")
        _validate_fingerprint(self.model_config_fingerprint, "model fingerprint")
        _validate_fingerprint(self.source_sha256, "source sha256")
        _validate_fingerprint(self.source_schema_fingerprint, "source schema fingerprint")
        _validate_relative_shard(self.shard)

    def _validate_cursor_binding(self) -> None:
        _validate_cursor(self.input_row_start, "input_row_start")
        _validate_cursor(self.input_row_end, "input_row_end")
        if self.input_row_end < self.input_row_start:
            raise CheckpointError("receipt input cursor moves backwards")
        if self.input_row_end == self.input_row_start:
            raise CheckpointError("receipt input cursor must cover at least one input row")
        _part_offset(self.part_name)
        if self.part_name != part_name_for_offset(self.input_row_start):
            raise CheckpointError("receipt part name is not bound to its input cursor")

    @property
    def input_row_count(self) -> int:
        """Return how many input rows this part consumed."""
        return self.input_row_end - self.input_row_start

    @property
    def input_cursor(self) -> int:
        """Return the input cursor reached after this part."""
        return self.input_row_end

    def to_payload(self) -> dict[str, object]:
        return {
            "receipt_schema_version": RECEIPT_SCHEMA_VERSION,
            "snapshot_id": self.snapshot_id,
            "model_config_fingerprint": self.model_config_fingerprint,
            "shard": self.shard,
            "input_row_start": self.input_row_start,
            "input_row_end": self.input_row_end,
            "part_name": self.part_name,
            "part_sha256": self.part_sha256,
            "row_count": self.row_count,
            "source_sha256": self.source_sha256,
            "source_schema_fingerprint": self.source_schema_fingerprint,
            "annotation_schema_version": self.annotation_schema_version,
        }

    @classmethod
    def from_payload(cls, payload: object) -> "PartReceipt":
        """Rebuild a receipt, rejecting any missing or wrongly typed field."""
        reader = require_object(payload, error=CheckpointError, label="receipt")
        _validate_schema_version(reader, "receipt_schema_version", RECEIPT_SCHEMA_VERSION)
        return cls(
            snapshot_id=reader.text("snapshot_id"),
            model_config_fingerprint=reader.text("model_config_fingerprint"),
            shard=reader.text("shard"),
            input_row_start=reader.integer("input_row_start"),
            input_row_end=reader.integer("input_row_end"),
            part_name=reader.text("part_name"),
            part_sha256=reader.text("part_sha256"),
            row_count=reader.integer("row_count"),
            source_sha256=reader.text("source_sha256"),
            source_schema_fingerprint=reader.text("source_schema_fingerprint"),
            annotation_schema_version=reader.integer("annotation_schema_version"),
        )


def _validate_schema_version(reader: PayloadReader, key: str, expected: int) -> None:
    if reader.integer(key) != expected:
        raise CheckpointError(f"unsupported {key.replace('_', ' ')}")


def _validated_parts(completed_parts: tuple[str, ...]) -> tuple[str, ...]:
    parts = tuple(completed_parts)
    for part in parts:
        _part_offset(part)
    if len(set(parts)) != len(parts):
        raise CheckpointError("checkpoint contains duplicate completed parts")
    # Every name validated above is ``part-<fixed-width zero-padded digits>``,
    # so lexicographic order is cursor order and needs no sort key.
    if parts != tuple(sorted(parts)):
        raise CheckpointError("checkpoint completed parts are not cursor ordered")
    return parts


def _shard_status(value: ShardStatus | str) -> ShardStatus:
    if isinstance(value, ShardStatus):
        return value
    try:
        return ShardStatus(value)
    except ValueError as error:
        raise CheckpointError(f"unsupported checkpoint status: {value!r}") from error


@dataclass(frozen=True, slots=True)
class ShardCheckpoint:
    """Latest durable cursor for one shard, ordered by committed part names."""

    snapshot_id: str
    model_config_fingerprint: str
    shard: str
    batch_size: int
    input_row_count: int
    input_cursor: int
    annotation_count: int
    completed_parts: tuple[str, ...]
    status: ShardStatus

    def __post_init__(self) -> None:
        _validate_fingerprint(self.snapshot_id, "snapshot id")
        _validate_fingerprint(self.model_config_fingerprint, "model fingerprint")
        _validate_relative_shard(self.shard)
        object.__setattr__(self, "status", _shard_status(self.status))
        object.__setattr__(self, "completed_parts", _validated_parts(self.completed_parts))
        self._validate_counts()

    def _validate_counts(self) -> None:
        _validate_batch_size(self.batch_size)
        _validate_cursor(self.input_row_count, "input_row_count")
        _validate_cursor(self.input_cursor, "input_cursor")
        _validate_cursor(self.annotation_count, "checkpoint annotation_count")
        _validate_checkpoint_cursor(self.input_row_count, self.input_cursor, self.batch_size)
        _validate_checkpoint_parts(self.completed_parts, self.input_cursor, self.batch_size)
        _validate_checkpoint_status(self)

    @property
    def is_complete(self) -> bool:
        """Return whether the shard's input is fully consumed."""
        return self.status is ShardStatus.COMPLETE

    def to_payload(self) -> dict[str, object]:
        return {
            "checkpoint_schema_version": CHECKPOINT_SCHEMA_VERSION,
            "snapshot_id": self.snapshot_id,
            "model_config_fingerprint": self.model_config_fingerprint,
            "shard": self.shard,
            "batch_size": self.batch_size,
            "input_row_count": self.input_row_count,
            "input_cursor": self.input_cursor,
            "annotation_count": self.annotation_count,
            "completed_parts": list(self.completed_parts),
            "status": str(self.status),
        }

    @classmethod
    def from_payload(cls, payload: object) -> "ShardCheckpoint":
        """Rebuild a checkpoint, rejecting any missing or wrongly typed field."""
        reader = require_object(payload, error=CheckpointError, label="checkpoint")
        _validate_schema_version(reader, "checkpoint_schema_version", CHECKPOINT_SCHEMA_VERSION)
        return cls(
            snapshot_id=reader.text("snapshot_id"),
            model_config_fingerprint=reader.text("model_config_fingerprint"),
            shard=reader.text("shard"),
            batch_size=reader.integer("batch_size"),
            input_row_count=reader.integer("input_row_count"),
            input_cursor=reader.integer("input_cursor"),
            annotation_count=reader.integer("annotation_count"),
            completed_parts=reader.texts("completed_parts"),
            status=_shard_status(reader.text("status")),
        )


def _validate_checkpoint_cursor(input_row_count: int, input_cursor: int, batch_size: int) -> None:
    if input_cursor > input_row_count:
        raise CheckpointError("checkpoint input cursor exceeds input row count")
    if input_cursor < input_row_count and input_cursor % batch_size:
        raise CheckpointError("checkpoint input cursor is not on a batch boundary")


def _validate_checkpoint_parts(
    completed_parts: tuple[str, ...], input_cursor: int, batch_size: int
) -> None:
    expected_part_count = (input_cursor + batch_size - 1) // batch_size
    if len(completed_parts) != expected_part_count:
        raise CheckpointError("checkpoint parts do not cover the input cursor")
    for index, part_name in enumerate(completed_parts):
        if _part_offset(part_name) != index * batch_size:
            raise CheckpointError("checkpoint parts do not cover the input cursor")


def _validate_checkpoint_status(checkpoint: ShardCheckpoint) -> None:
    if checkpoint.is_complete and checkpoint.input_cursor != checkpoint.input_row_count:
        raise CheckpointError("complete checkpoint does not cover the input")
    if not checkpoint.is_complete and checkpoint.input_cursor == checkpoint.input_row_count:
        raise CheckpointError("paused checkpoint covers the input")


def validate_receipt_chain(
    checkpoint: ShardCheckpoint,
    receipts: Iterator[PartReceipt],
    *,
    source_sha256: str,
    source_schema_fingerprint: str,
) -> None:
    """Validate every committed receipt against one checkpoint and source.

    Part names encode the input start offset, but only the receipts carry the
    end offsets. Validate the complete chain before a worker trusts its
    checkpoint cursor; otherwise a structurally valid checkpoint can skip an
    unlisted interval of input rows.
    """
    receipt_list = tuple(receipts)
    _validate_receipt_count(checkpoint, receipt_list)
    expected_cursor = 0
    # Length equality is validated above with the domain-specific diagnostic.
    for part_name, receipt in zip(checkpoint.completed_parts, receipt_list):  # noqa: B905
        _validate_receipt_binding(
            receipt,
            checkpoint,
            part_name,
            source_sha256=source_sha256,
            source_schema_fingerprint=source_schema_fingerprint,
        )
        if receipt.input_row_start != expected_cursor:
            raise CheckpointError(f"committed receipts are not contiguous at {part_name}")
        expected_cursor = receipt.input_row_end
    if expected_cursor != checkpoint.input_cursor:
        raise CheckpointError("committed receipts do not end at the checkpoint cursor")


def _validate_receipt_count(checkpoint: ShardCheckpoint, receipts: tuple[PartReceipt, ...]) -> None:
    if len(receipts) != len(checkpoint.completed_parts):
        raise CheckpointError("checkpoint parts and receipts do not have the same length")


def _validate_receipt_binding(
    receipt: PartReceipt,
    checkpoint: ShardCheckpoint,
    part_name: str,
    *,
    source_sha256: str,
    source_schema_fingerprint: str,
) -> None:
    checks = (
        (
            receipt.part_name != part_name,
            f"receipt part name does not match checkpoint: {part_name}",
        ),
        (
            receipt.snapshot_id != checkpoint.snapshot_id,
            f"receipt snapshot does not match checkpoint: {part_name}",
        ),
        (
            receipt.model_config_fingerprint != checkpoint.model_config_fingerprint,
            f"receipt model does not match checkpoint: {part_name}",
        ),
        (
            receipt.shard != checkpoint.shard,
            f"receipt shard does not match checkpoint: {part_name}",
        ),
        (
            receipt.source_sha256 != source_sha256,
            f"receipt source does not match snapshot: {part_name}",
        ),
        (
            receipt.source_schema_fingerprint != source_schema_fingerprint,
            f"receipt source schema does not match snapshot: {part_name}",
        ),
    )
    message = next((message for mismatch, message in checks if mismatch), None)
    if message is not None:
        raise CheckpointError(message)


def write_checkpoint(path: Path, checkpoint: ShardCheckpoint) -> None:
    """Atomically persist a checkpoint after fsyncing content and directory."""
    atomic_write_json(path, checkpoint.to_payload())


def write_receipt(path: Path, receipt: PartReceipt) -> None:
    """Atomically persist one part receipt after the part is committed."""
    atomic_write_json(path, receipt.to_payload())


def _read_json_object(path: Path, label: str) -> Mapping[str, object]:
    if path.is_symlink():
        raise CheckpointError(f"{label} must not be a symlink: {path}")
    try:
        text = path.read_text(encoding="utf-8")  # pragma: no mutate - codec alias only
        payload = json.loads(text)
    except (OSError, UnicodeError) as error:
        raise CheckpointError(f"cannot read {label} {path}: {error}") from error
    except json.JSONDecodeError as error:
        raise CheckpointError(f"corrupt {label} JSON {path}: {error}") from error
    if not isinstance(payload, Mapping):
        raise CheckpointError(f"{label} payload must be an object: {path}")
    return payload


def read_checkpoint(path: Path) -> ShardCheckpoint:
    """Read one checkpoint and reject malformed or stale-shaped JSON."""
    return ShardCheckpoint.from_payload(_read_json_object(path, "checkpoint"))


def read_receipt(path: Path) -> PartReceipt:
    """Read one receipt and reject malformed or stale-shaped JSON."""
    return PartReceipt.from_payload(_read_json_object(path, "receipt"))


@contextmanager
def exclusive_worker_lock(run_dir: Path) -> Iterator[None]:
    """Hold an advisory non-blocking lock for the lifetime of one worker."""
    if run_dir.is_symlink():
        raise CheckpointError(f"run directory must not be a symlink: {run_dir}")
    run_dir.mkdir(parents=True, exist_ok=True)
    lock_path = run_dir / WORKER_LOCK_FILENAME
    if lock_path.is_symlink():
        raise CheckpointError(f"worker lock must not be a symlink: {lock_path}")
    try:
        handle = lock_path.open("a+")
    except OSError as error:
        raise CheckpointError(f"cannot open worker lock {lock_path}: {error}") from error
    try:
        _acquire_lock(handle.fileno(), run_dir)
        try:
            yield
        finally:
            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
    finally:
        handle.close()


def _acquire_lock(descriptor: int, run_dir: Path) -> None:
    try:
        fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError as error:
        if error.errno in {errno.EACCES, errno.EAGAIN}:
            raise WorkerBusyError(f"worker run is already locked: {run_dir}") from error
        raise CheckpointError(f"cannot lock worker run {run_dir}: {error}") from error


__all__ = [
    "ANNOTATION_SCHEMA_VERSION",
    "CHECKPOINT_SCHEMA_VERSION",
    "MAX_BATCH_SIZE",
    "RECEIPT_SCHEMA_VERSION",
    "WORKER_LOCK_FILENAME",
    "CheckpointError",
    "PartReceipt",
    "ShardCheckpoint",
    "ShardPaths",
    "ShardStatus",
    "WorkerBusyError",
    "exclusive_worker_lock",
    "is_generated_part_name",
    "is_generated_receipt_name",
    "part_name_for_offset",
    "read_checkpoint",
    "read_receipt",
    "receipt_name_for_part",
    "shard_paths",
    "validate_receipt_chain",
    "write_checkpoint",
    "write_receipt",
]
