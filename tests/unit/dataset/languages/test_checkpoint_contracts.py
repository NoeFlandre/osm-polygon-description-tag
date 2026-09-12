"""Exact operator-facing contracts of the shard checkpoint module.

The tests elsewhere in this package assert that the right refusal happens.
These assert *what the operator is told* and *what the run directory is
named*, by exact value: a substring match still accepts a corrupted message,
and a case-insensitive filesystem still accepts a corrupted directory name.
Both are load-bearing, because an operator triages a stalled Grid'5000 shard
from the message and resumes it from the path.
"""

import hashlib
from pathlib import Path
from typing import Any

import pytest

from osm_polygon_description_tag.dataset.languages.checkpoint import (
    MAX_BATCH_SIZE,
    CheckpointError,
    ShardCheckpoint,
    ShardStatus,
    part_name_for_offset,
    receipt_name_for_part,
    shard_paths,
)

_SNAPSHOT = "a" * 64
_FINGERPRINT = "b" * 64


def _checkpoint(**overrides: Any) -> ShardCheckpoint:
    """Build a checkpoint, letting each test state exactly one bad field."""
    defaults: dict[str, Any] = {
        "snapshot_id": _SNAPSHOT,
        "model_config_fingerprint": _FINGERPRINT,
        "shard": "region.parquet",
        "batch_size": 4,
        "input_row_count": 12,
        "input_cursor": 4,
        "annotation_count": 3,
        "completed_parts": (part_name_for_offset(0),),
        "status": ShardStatus.PAUSED,
    }
    return ShardCheckpoint(**{**defaults, **overrides})


@pytest.mark.parametrize(
    ("overrides", "message"),
    [
        (
            {"completed_parts": ("part.parquet",)},
            "part name must be a relative part name",
        ),
        ({"batch_size": 0}, "checkpoint batch_size must be positive"),
        (
            {"batch_size": MAX_BATCH_SIZE + 1},
            f"checkpoint batch_size must not exceed {MAX_BATCH_SIZE}",
        ),
        (
            {"input_row_count": 2, "input_cursor": 4},
            "checkpoint input cursor exceeds input row count",
        ),
        (
            {"input_cursor": 5, "completed_parts": (part_name_for_offset(0),)},
            "checkpoint input cursor is not on a batch boundary",
        ),
        (
            {"input_cursor": 8, "completed_parts": (part_name_for_offset(0),)},
            "checkpoint parts do not cover the input cursor",
        ),
        (
            {
                "input_cursor": 12,
                "status": ShardStatus.PAUSED,
                "completed_parts": tuple(part_name_for_offset(o) for o in (0, 4, 8)),
            },
            "paused checkpoint covers the input",
        ),
        (
            {"input_cursor": 4, "status": ShardStatus.COMPLETE},
            "complete checkpoint does not cover the input",
        ),
        ({"shard": "region.txt"}, "shard must be a Parquet path"),
        (
            {
                "input_cursor": 8,
                "completed_parts": (part_name_for_offset(4), part_name_for_offset(0)),
            },
            "checkpoint completed parts are not cursor ordered",
        ),
        (
            {
                "input_cursor": 8,
                "completed_parts": (part_name_for_offset(0), part_name_for_offset(0)),
            },
            "checkpoint contains duplicate completed parts",
        ),
    ],
)
def test_checkpoint_refusals_state_their_reason_exactly(
    overrides: dict[str, Any], message: str
) -> None:
    with pytest.raises(CheckpointError) as caught:
        _checkpoint(**overrides)

    assert str(caught.value) == message


def test_a_batch_size_at_the_documented_maximum_is_accepted() -> None:
    """The maximum is inclusive, so the boundary itself must not be refused."""
    checkpoint = _checkpoint(
        batch_size=MAX_BATCH_SIZE,
        input_row_count=MAX_BATCH_SIZE * 2,
        input_cursor=MAX_BATCH_SIZE,
        completed_parts=(part_name_for_offset(0),),
    )

    assert checkpoint.batch_size == MAX_BATCH_SIZE


@pytest.mark.parametrize(
    ("offset", "message"),
    [
        (-1, "input_row_offset must be a non-negative integer"),
        (10**20, "input row offset is too large for a part name"),
    ],
)
def test_part_name_refusals_name_the_offset_they_refused(offset: int, message: str) -> None:
    with pytest.raises(CheckpointError) as caught:
        part_name_for_offset(offset)

    assert str(caught.value) == message


def test_receipt_name_refuses_a_name_that_is_not_a_generated_part() -> None:
    with pytest.raises(CheckpointError) as caught:
        receipt_name_for_part("part-4.parquet")

    assert str(caught.value) == "part name must be a relative part name"


def test_shard_directories_are_named_by_the_utf8_sha256_of_the_shard(tmp_path: Path) -> None:
    """Pin the directory identity exactly: the encoding, the digest, and the width.

    A shard key is how a resumed Grid'5000 job finds its own state, so a
    different encoding, a different digest, or a different truncation width
    would silently orphan committed parts.
    """
    shard = "régions/île-de-france.parquet"
    expected = hashlib.sha256(shard.encode("utf-8")).hexdigest()[:32]

    run_dir = tmp_path / "run"
    paths = shard_paths(run_dir, shard)

    assert paths.root.name == expected
    assert len(paths.root.name) == 32
    assert paths.root.parent == run_dir / "shards"


def test_the_shards_directory_component_is_lowercase_on_disk(tmp_path: Path) -> None:
    """macOS is case-insensitive, so compare the name the run directory holds.

    ``Path.exists`` would accept ``SHARDS`` here; listing the directory is what
    proves the run directory is portable to the case-sensitive Linux compute
    nodes that actually execute the shards.
    """
    run_dir = tmp_path / "run"
    paths = shard_paths(run_dir, "region.parquet")
    paths.parts.mkdir(parents=True)

    assert [entry.name for entry in run_dir.iterdir()] == ["shards"]
