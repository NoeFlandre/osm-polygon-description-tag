"""Focused receipt-chain contract cases for mutation survivors."""

from __future__ import annotations

from collections.abc import Iterator
from typing import Any

import pytest

from osm_polygon_description_tag.dataset.languages.checkpoint import (
    CheckpointError,
    PartReceipt,
    ShardCheckpoint,
    ShardStatus,
    part_name_for_offset,
    validate_receipt_chain,
)

_SNAPSHOT_ID = "a" * 64
_MODEL_FINGERPRINT = "b" * 64
_SOURCE_SHA256 = "d" * 64
_SOURCE_SCHEMA_FINGERPRINT = "e" * 64


@pytest.fixture
def checkpoint() -> ShardCheckpoint:
    return ShardCheckpoint(
        snapshot_id=_SNAPSHOT_ID,
        model_config_fingerprint=_MODEL_FINGERPRINT,
        shard="region.parquet",
        batch_size=4,
        input_row_count=8,
        input_cursor=8,
        annotation_count=5,
        completed_parts=(part_name_for_offset(0), part_name_for_offset(4)),
        status=ShardStatus.COMPLETE,
    )


def _receipt(start: int, end: int, row_count: int, **overrides: Any) -> PartReceipt:
    values: dict[str, Any] = {
        "snapshot_id": _SNAPSHOT_ID,
        "model_config_fingerprint": _MODEL_FINGERPRINT,
        "shard": "region.parquet",
        "input_row_start": start,
        "input_row_end": end,
        "part_name": part_name_for_offset(start),
        "part_sha256": "c" * 64,
        "row_count": row_count,
        "source_sha256": _SOURCE_SHA256,
        "source_schema_fingerprint": _SOURCE_SCHEMA_FINGERPRINT,
    }
    values.update(overrides)
    return PartReceipt(**values)


@pytest.fixture
def valid_receipts() -> tuple[PartReceipt, PartReceipt]:
    return (_receipt(0, 4, 2), _receipt(4, 8, 3))


def test_receipt_chain_accepts_a_bound_contiguous_chain_ending_at_checkpoint_cursor(
    checkpoint: ShardCheckpoint,
    valid_receipts: tuple[PartReceipt, PartReceipt],
) -> None:
    assert len(valid_receipts) == len(checkpoint.completed_parts)

    assert (
        validate_receipt_chain(
            checkpoint,
            iter(valid_receipts),
            source_sha256=_SOURCE_SHA256,
            source_schema_fingerprint=_SOURCE_SCHEMA_FINGERPRINT,
        )
        is None
    )


@pytest.mark.parametrize(
    ("override", "expected_message"),
    [
        ({"snapshot_id": "f" * 64}, "receipt snapshot does not match checkpoint"),
        (
            {"model_config_fingerprint": "f" * 64},
            "receipt model does not match checkpoint",
        ),
        ({"shard": "other.parquet"}, "receipt shard does not match checkpoint"),
        ({"source_sha256": "f" * 64}, "receipt source does not match snapshot"),
        (
            {"source_schema_fingerprint": "f" * 64},
            "receipt source schema does not match snapshot",
        ),
    ],
)
def test_receipt_chain_rejects_a_valid_receipt_bound_to_another_run_or_source(
    checkpoint: ShardCheckpoint,
    override: dict[str, object],
    expected_message: str,
) -> None:
    receipts: Iterator[PartReceipt] = iter((_receipt(0, 4, 2, **override), _receipt(4, 8, 3)))

    with pytest.raises(CheckpointError, match=expected_message):
        validate_receipt_chain(
            checkpoint,
            receipts,
            source_sha256=_SOURCE_SHA256,
            source_schema_fingerprint=_SOURCE_SCHEMA_FINGERPRINT,
        )


def test_receipt_chain_binds_receipt_order_to_checkpoint_parts(
    checkpoint: ShardCheckpoint,
    valid_receipts: tuple[PartReceipt, PartReceipt],
) -> None:
    with pytest.raises(
        CheckpointError,
        match="receipt part name does not match checkpoint: part-00000000000000000000\\.parquet",
    ):
        validate_receipt_chain(
            checkpoint,
            reversed(valid_receipts),
            source_sha256=_SOURCE_SHA256,
            source_schema_fingerprint=_SOURCE_SCHEMA_FINGERPRINT,
        )


def test_receipt_chain_rejects_a_gap_between_individually_valid_receipts(
    checkpoint: ShardCheckpoint,
) -> None:
    receipts = iter((_receipt(0, 3, 2), _receipt(4, 8, 3)))

    with pytest.raises(
        CheckpointError,
        match="committed receipts are not contiguous at part-00000000000000000004\\.parquet",
    ):
        validate_receipt_chain(
            checkpoint,
            receipts,
            source_sha256=_SOURCE_SHA256,
            source_schema_fingerprint=_SOURCE_SCHEMA_FINGERPRINT,
        )
