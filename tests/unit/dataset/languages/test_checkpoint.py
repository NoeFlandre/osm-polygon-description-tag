"""Durable checkpoints, part receipts, and exclusive worker locking."""

import json
import os
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest

from osm_polygon_description_tag.dataset.languages.checkpoint import (
    MAX_BATCH_SIZE,
    CheckpointError,
    PartReceipt,
    ShardCheckpoint,
    ShardStatus,
    WorkerBusyError,
    exclusive_worker_lock,
    part_name_for_offset,
    read_checkpoint,
    read_receipt,
    receipt_name_for_part,
    shard_paths,
    validate_receipt_chain,
    write_checkpoint,
    write_receipt,
)

_PART = "part-00000000000000000004.parquet"


def _receipt(**overrides: Any) -> PartReceipt:
    defaults: dict[str, Any] = {
        "snapshot_id": "a" * 64,
        "model_config_fingerprint": "b" * 64,
        "shard": "region.parquet",
        "input_row_start": 4,
        "input_row_end": 8,
        "part_name": _PART,
        "part_sha256": "c" * 64,
        "row_count": 3,
        "source_sha256": "d" * 64,
        "source_schema_fingerprint": "e" * 64,
    }
    return PartReceipt(**{**defaults, **overrides})


def _checkpoint(**overrides: Any) -> ShardCheckpoint:
    defaults: dict[str, Any] = {
        "snapshot_id": "a" * 64,
        "model_config_fingerprint": "b" * 64,
        "shard": "region.parquet",
        "batch_size": 4,
        "input_row_count": 12,
        "input_cursor": 8,
        "annotation_count": 3,
        "completed_parts": (_PART,),
        "status": ShardStatus.PAUSED,
    }
    values = {**defaults, **overrides}
    if "completed_parts" not in overrides:
        input_cursor = values["input_cursor"]
        batch_size = values["batch_size"]
        if (
            type(input_cursor) is int
            and input_cursor >= 0
            and type(batch_size) is int
            and batch_size > 0
        ):
            values["completed_parts"] = tuple(
                part_name_for_offset(offset) for offset in range(0, input_cursor, batch_size)
            )
    return ShardCheckpoint(**values)


def _rewrite(path: Path, payload: dict[str, Any], mutate: Callable[[dict[str, Any]], None]) -> None:
    mutate(payload)
    path.write_text(json.dumps(payload), encoding="utf-8")


def test_checkpoint_rejects_correct_part_count_with_wrong_offsets() -> None:
    with pytest.raises(CheckpointError) as caught:
        _checkpoint(completed_parts=(part_name_for_offset(0), part_name_for_offset(8)))
    assert str(caught.value) == "checkpoint parts do not cover the input cursor"


@pytest.mark.parametrize("receipt_count", [1, 3])
def test_receipt_chain_rejects_missing_or_extra_receipts(receipt_count: int) -> None:
    receipts = (
        _receipt(
            input_row_start=offset, input_row_end=offset + 4, part_name=part_name_for_offset(offset)
        )
        for offset in range(0, receipt_count * 4, 4)
    )
    with pytest.raises(CheckpointError) as caught:
        validate_receipt_chain(
            _checkpoint(),
            receipts,
            source_sha256="d" * 64,
            source_schema_fingerprint="e" * 64,
        )
    assert str(caught.value) == "checkpoint parts and receipts do not have the same length"


def test_contiguous_receipts_must_reach_the_checkpoint_cursor() -> None:
    receipts = iter(
        (
            _receipt(input_row_start=0, input_row_end=4, part_name=part_name_for_offset(0)),
            _receipt(input_row_start=4, input_row_end=7),
        )
    )
    with pytest.raises(CheckpointError) as caught:
        validate_receipt_chain(
            _checkpoint(),
            receipts,
            source_sha256="d" * 64,
            source_schema_fingerprint="e" * 64,
        )
    assert str(caught.value) == "committed receipts do not end at the checkpoint cursor"


def test_checkpoint_and_receipt_round_trip_with_cursor_bindings(tmp_path: Path) -> None:
    paths = shard_paths(tmp_path, "region.parquet")
    write_checkpoint(paths.checkpoint, _checkpoint())
    write_receipt(paths.receipt(_PART), _receipt())

    assert read_checkpoint(paths.checkpoint) == _checkpoint()
    assert read_receipt(paths.receipts / "part-00000000000000000004.json") == _receipt()
    assert _receipt().input_row_count == 4
    assert _receipt().input_cursor == 8
    assert part_name_for_offset(4) == _PART
    assert receipt_name_for_part(_PART) == "part-00000000000000000004.json"
    assert paths.part(_PART) == paths.parts / _PART
    assert not _checkpoint().is_complete
    assert _checkpoint(input_cursor=12, status=ShardStatus.COMPLETE).is_complete


def test_shard_paths_are_deterministic_and_isolated(tmp_path: Path) -> None:
    first = shard_paths(tmp_path, "a/region.parquet")
    again = shard_paths(tmp_path, "a/region.parquet")
    other = shard_paths(tmp_path, "b/region.parquet")

    assert first == again
    assert first.root != other.root
    assert not first.root.exists()


def test_checkpoint_status_accepts_its_string_spelling(tmp_path: Path) -> None:
    assert _checkpoint(status="paused").status is ShardStatus.PAUSED
    assert _checkpoint(input_cursor=12, status="complete").status is ShardStatus.COMPLETE

    with pytest.raises(CheckpointError, match="paused checkpoint covers the input"):
        _checkpoint(input_cursor=12, status="paused")


def test_reading_state_rejects_corrupt_and_non_object_documents(tmp_path: Path) -> None:
    paths = shard_paths(tmp_path, "region.parquet")
    paths.checkpoint.parent.mkdir(parents=True)
    paths.checkpoint.write_text("{not-json", encoding="utf-8")
    with pytest.raises(CheckpointError, match="corrupt checkpoint JSON"):
        read_checkpoint(paths.checkpoint)

    paths.checkpoint.write_text("[]", encoding="utf-8")
    with pytest.raises(CheckpointError, match="checkpoint payload must be an object"):
        read_checkpoint(paths.checkpoint)

    with pytest.raises(CheckpointError, match="cannot read receipt"):
        read_receipt(tmp_path / "absent.json")

    receipt_path = tmp_path / "receipt.json"
    receipt_path.write_text("{not-json", encoding="utf-8")
    with pytest.raises(CheckpointError, match="corrupt receipt JSON"):
        read_receipt(receipt_path)


def test_read_checkpoint_rejects_a_valid_symlinked_checkpoint(tmp_path: Path) -> None:
    paths = shard_paths(tmp_path, "region.parquet")
    target = tmp_path / "external-checkpoint.json"
    write_checkpoint(target, _checkpoint())
    paths.checkpoint.parent.mkdir(parents=True)
    paths.checkpoint.symlink_to(target)

    with pytest.raises(CheckpointError, match="checkpoint must not be a symlink"):
        read_checkpoint(paths.checkpoint)


def test_checkpoint_rejects_a_cursor_beyond_the_input(tmp_path: Path) -> None:
    path = tmp_path / "checkpoint.json"
    _rewrite(path, _checkpoint().to_payload(), lambda p: p.__setitem__("input_cursor", 13))

    with pytest.raises(CheckpointError, match="cursor exceeds input row count"):
        read_checkpoint(path)


def test_complete_checkpoint_must_cover_the_whole_input() -> None:
    with pytest.raises(CheckpointError, match="complete checkpoint does not cover the input"):
        _checkpoint(status=ShardStatus.COMPLETE)


@pytest.mark.parametrize(
    ("key", "value", "message"),
    [
        ("checkpoint_schema_version", 2, "unsupported checkpoint schema version"),
        ("checkpoint_schema_version", "1", "must be an integer"),
        ("snapshot_id", 1, "must be a string"),
        ("batch_size", 4.0, "must be an integer"),
        ("input_cursor", True, "must be an integer"),
        ("completed_parts", _PART, "must be a list"),
        ("completed_parts", [7], "must contain only strings"),
        ("status", "running", "unsupported checkpoint status"),
        ("batch_size", MAX_BATCH_SIZE + 1, "must not exceed"),
    ],
)
def test_checkpoint_payload_rejects_malformed_fields(
    tmp_path: Path, key: str, value: object, message: str
) -> None:
    path = tmp_path / "checkpoint.json"
    _rewrite(path, _checkpoint().to_payload(), lambda p: p.__setitem__(key, value))

    with pytest.raises(CheckpointError, match=message):
        read_checkpoint(path)


def test_checkpoint_payload_rejects_missing_fields(tmp_path: Path) -> None:
    path = tmp_path / "checkpoint.json"
    _rewrite(path, _checkpoint().to_payload(), lambda p: p.pop("annotation_count"))

    with pytest.raises(CheckpointError, match="missing annotation_count"):
        read_checkpoint(path)


@pytest.mark.parametrize(
    ("kwargs", "message"),
    [
        ({"batch_size": 0}, "batch_size must be positive"),
        ({"batch_size": True}, "batch_size must be positive"),
        ({"annotation_count": -1}, "annotation_count must be a non-negative integer"),
        ({"input_cursor": -1}, "input_cursor must be a non-negative integer"),
        ({"input_cursor": 12, "status": ShardStatus.PAUSED}, "paused checkpoint covers the input"),
        ({"snapshot_id": "short"}, "snapshot id must be a lowercase"),
        ({"model_config_fingerprint": "X" * 64}, "model fingerprint must be a lowercase"),
        ({"shard": ""}, "shard must be a non-empty relative path"),
        ({"shard": "/abs.parquet"}, "shard must be relative and portable"),
        ({"shard": "../a.parquet"}, "shard must not contain traversal"),
        ({"shard": "region.txt"}, "shard must be a Parquet path"),
        ({"shard": 7}, "shard must be a non-empty relative path"),
        ({"completed_parts": (_PART, _PART)}, "duplicate completed parts"),
        ({"completed_parts": ("part.parquet",)}, "part name must be a relative part name"),
    ],
)
def test_checkpoint_validates_its_fields(kwargs: dict[str, object], message: str) -> None:
    with pytest.raises(CheckpointError, match=message):
        _checkpoint(**kwargs)


def test_checkpoint_requires_cursor_ordered_parts() -> None:
    later = part_name_for_offset(8)

    with pytest.raises(CheckpointError, match="not cursor ordered"):
        _checkpoint(completed_parts=(later, _PART))


def test_checkpoint_requires_parts_to_cover_its_cursor() -> None:
    with pytest.raises(CheckpointError, match="parts do not cover"):
        _checkpoint(completed_parts=(_PART,), input_cursor=8)


@pytest.mark.parametrize(
    ("kwargs", "message"),
    [
        ({"input_row_start": -1}, "input_row_start must be a non-negative integer"),
        ({"input_row_end": 2}, "input cursor moves backwards"),
        ({"input_row_end": 4}, "must cover at least one input row"),
        ({"part_name": "../part.parquet"}, "part name must be a relative part name"),
        ({"part_name": part_name_for_offset(6)}, "not bound to its input cursor"),
        ({"part_sha256": "nothex"}, "part sha256 must be a lowercase"),
        ({"row_count": -1}, "row_count must be a non-negative integer"),
        ({"annotation_schema_version": 2}, "unsupported annotation schema version"),
        ({"source_sha256": ""}, "source sha256 must be a lowercase"),
    ],
)
def test_receipt_validates_its_fields(kwargs: dict[str, object], message: str) -> None:
    with pytest.raises(CheckpointError, match=message):
        _receipt(**kwargs)


def test_receipt_rejects_unsafe_part_names(tmp_path: Path) -> None:
    path = tmp_path / "receipt.json"
    _rewrite(path, _receipt().to_payload(), lambda p: p.__setitem__("part_name", "../part.parquet"))

    with pytest.raises(CheckpointError, match="part name must be a relative part name"):
        read_receipt(path)


def test_receipt_payload_rejects_a_stale_schema_version(tmp_path: Path) -> None:
    path = tmp_path / "receipt.json"
    _rewrite(path, _receipt().to_payload(), lambda p: p.__setitem__("receipt_schema_version", 99))

    with pytest.raises(CheckpointError, match="unsupported receipt schema version"):
        read_receipt(path)


def test_part_names_are_fixed_width_and_bounded() -> None:
    assert part_name_for_offset(0) == "part-00000000000000000000.parquet"
    assert part_name_for_offset(10**20 - 1) == f"part-{10**20 - 1}.parquet"

    with pytest.raises(CheckpointError, match="too large for a part name"):
        part_name_for_offset(10**20)
    with pytest.raises(CheckpointError, match="must be a non-negative integer"):
        part_name_for_offset(-1)
    with pytest.raises(CheckpointError, match="part name must be a relative part name"):
        receipt_name_for_part("part-4.parquet")


def test_writes_are_atomic_and_leave_no_temporary_files(tmp_path: Path) -> None:
    paths = shard_paths(tmp_path, "region.parquet")
    write_checkpoint(paths.checkpoint, _checkpoint())
    write_checkpoint(paths.checkpoint, _checkpoint(input_cursor=12, status=ShardStatus.COMPLETE))

    assert read_checkpoint(paths.checkpoint).is_complete
    assert [item.name for item in paths.checkpoint.parent.iterdir()] == ["checkpoint.json"]


def test_a_partial_write_never_replaces_committed_state(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    paths = shard_paths(tmp_path, "region.parquet")
    write_checkpoint(paths.checkpoint, _checkpoint())

    def _explode(source: object, target: object) -> None:
        raise OSError("simulated crash during rename")

    monkeypatch.setattr(os, "replace", _explode)
    with pytest.raises(OSError, match="simulated crash"):
        write_checkpoint(paths.checkpoint, _checkpoint(input_cursor=12, status="complete"))

    monkeypatch.undo()
    assert read_checkpoint(paths.checkpoint) == _checkpoint()
    assert [item.name for item in paths.checkpoint.parent.iterdir()] == ["checkpoint.json"]


def test_exclusive_worker_lock_rejects_a_second_local_worker(tmp_path: Path) -> None:
    with (
        exclusive_worker_lock(tmp_path),
        pytest.raises(WorkerBusyError, match="already locked"),
        exclusive_worker_lock(tmp_path),
    ):
        pass


def test_worker_lock_is_released_after_use(tmp_path: Path) -> None:
    with exclusive_worker_lock(tmp_path):
        pass

    with exclusive_worker_lock(tmp_path):
        pass


def test_worker_lock_is_released_when_the_body_raises(tmp_path: Path) -> None:
    with pytest.raises(RuntimeError, match="worker failed"), exclusive_worker_lock(tmp_path):
        raise RuntimeError("worker failed")

    with exclusive_worker_lock(tmp_path):
        pass


def test_worker_lock_rejects_a_symlinked_run_directory(tmp_path: Path) -> None:
    real = tmp_path / "real"
    real.mkdir()
    linked = tmp_path / "linked"
    linked.symlink_to(real)

    with (
        pytest.raises(CheckpointError, match="must not be a symlink"),
        exclusive_worker_lock(linked),
    ):
        pass


def test_worker_lock_rejects_a_valid_symlinked_lock_file(tmp_path: Path) -> None:
    run_dir = tmp_path / "run"
    run_dir.mkdir()
    target = tmp_path / "external.lock"
    target.write_text("sentinel", encoding="utf-8")
    (run_dir / ".worker.lock").symlink_to(target)

    with (
        pytest.raises(CheckpointError, match="worker lock must not be a symlink"),
        exclusive_worker_lock(run_dir),
    ):
        pass

    assert target.read_text(encoding="utf-8") == "sentinel"


def test_worker_lock_reports_an_unopenable_lock_file(tmp_path: Path) -> None:
    run_dir = tmp_path / "run"
    run_dir.mkdir()
    (run_dir / ".worker.lock").mkdir()

    with (
        pytest.raises(CheckpointError, match="cannot open worker lock"),
        exclusive_worker_lock(run_dir),
    ):
        pass


def test_an_unexpected_lock_failure_is_reported(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import errno
    import fcntl

    def _explode(descriptor: int, operation: int) -> None:
        raise OSError(errno.EPERM, "locking is not permitted here")

    monkeypatch.setattr(fcntl, "flock", _explode)

    with (
        pytest.raises(CheckpointError, match="cannot lock worker run"),
        exclusive_worker_lock(tmp_path),
    ):
        pass
