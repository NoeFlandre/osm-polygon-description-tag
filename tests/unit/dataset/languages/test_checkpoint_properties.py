"""Generated durability properties for shard checkpoint and receipt models."""

from __future__ import annotations

import tempfile
from pathlib import Path

from hypothesis import example, given
from hypothesis import strategies as st

from osm_polygon_description_tag.dataset.languages.checkpoint import (
    MAX_BATCH_SIZE,
    PartReceipt,
    ShardCheckpoint,
    ShardStatus,
    part_name_for_offset,
    read_checkpoint,
    read_receipt,
    write_checkpoint,
    write_receipt,
)

_FINGERPRINTS = st.text(alphabet="0123456789abcdef", min_size=64, max_size=64)
_SHARD = st.text(alphabet="abcdefghijklmnopqrstuvwxyz0123456789_-", min_size=1, max_size=12).map(
    lambda name: f"nested/{name}.parquet"
)


@st.composite
def _checkpoints(draw: st.DrawFn) -> ShardCheckpoint:
    batch_size = draw(st.integers(min_value=1, max_value=min(MAX_BATCH_SIZE, 32)))
    input_row_count = draw(st.integers(min_value=0, max_value=128))
    cursors = list(range(0, input_row_count // batch_size * batch_size + 1, batch_size))
    if cursors[-1] != input_row_count:
        cursors.append(input_row_count)
    input_cursor = draw(st.sampled_from(cursors))
    completed_parts = tuple(
        part_name_for_offset(offset) for offset in range(0, input_cursor, batch_size)
    )

    return ShardCheckpoint(
        snapshot_id=draw(_FINGERPRINTS),
        model_config_fingerprint=draw(_FINGERPRINTS),
        shard=draw(_SHARD),
        batch_size=batch_size,
        input_row_count=input_row_count,
        input_cursor=input_cursor,
        annotation_count=draw(st.integers(min_value=0, max_value=128)),
        completed_parts=completed_parts,
        status=(ShardStatus.COMPLETE if input_cursor == input_row_count else ShardStatus.PAUSED),
    )


def _checkpoint_example(*, row_count: int, cursor: int, batch_size: int) -> ShardCheckpoint:
    return ShardCheckpoint(
        snapshot_id="a" * 64,
        model_config_fingerprint="b" * 64,
        shard="nested/region.parquet",
        batch_size=batch_size,
        input_row_count=row_count,
        input_cursor=cursor,
        annotation_count=3,
        completed_parts=tuple(
            part_name_for_offset(offset) for offset in range(0, cursor, batch_size)
        ),
        status=ShardStatus.COMPLETE if cursor == row_count else ShardStatus.PAUSED,
    )


@given(checkpoint=_checkpoints())
@example(checkpoint=_checkpoint_example(row_count=0, cursor=0, batch_size=4))
@example(checkpoint=_checkpoint_example(row_count=12, cursor=8, batch_size=4))
@example(checkpoint=_checkpoint_example(row_count=11, cursor=11, batch_size=4))
def test_generated_checkpoints_round_trip_through_disk(checkpoint: ShardCheckpoint) -> None:
    with tempfile.TemporaryDirectory() as directory:
        path = Path(directory) / "checkpoint.json"

        write_checkpoint(path, checkpoint)

        assert read_checkpoint(path) == checkpoint


@st.composite
def _receipts(draw: st.DrawFn) -> PartReceipt:
    input_row_start = draw(st.integers(min_value=0, max_value=10**20 - 1))
    return PartReceipt(
        snapshot_id=draw(_FINGERPRINTS),
        model_config_fingerprint=draw(_FINGERPRINTS),
        shard=draw(_SHARD),
        input_row_start=input_row_start,
        input_row_end=input_row_start + draw(st.integers(min_value=1, max_value=128)),
        part_name=part_name_for_offset(input_row_start),
        part_sha256=draw(_FINGERPRINTS),
        row_count=draw(st.integers(min_value=0, max_value=128)),
        source_sha256=draw(_FINGERPRINTS),
        source_schema_fingerprint=draw(_FINGERPRINTS),
    )


def _receipt_example(input_row_start: int) -> PartReceipt:
    return PartReceipt(
        snapshot_id="a" * 64,
        model_config_fingerprint="b" * 64,
        shard="nested/region.parquet",
        input_row_start=input_row_start,
        input_row_end=input_row_start + 4,
        part_name=part_name_for_offset(input_row_start),
        part_sha256="c" * 64,
        row_count=3,
        source_sha256="d" * 64,
        source_schema_fingerprint="e" * 64,
    )


@given(receipt=_receipts())
@example(receipt=_receipt_example(0))
@example(receipt=_receipt_example(4))
@example(receipt=_receipt_example(10**20 - 1))
def test_generated_receipts_round_trip_through_disk(receipt: PartReceipt) -> None:
    with tempfile.TemporaryDirectory() as directory:
        path = Path(directory) / "receipt.json"

        write_receipt(path, receipt)

        assert read_receipt(path) == receipt
