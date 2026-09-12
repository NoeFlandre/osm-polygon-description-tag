"""Exact validation contracts for one language run directory.

Validation is the only thing standing between a damaged shard and a published
dataset, so the numbers it reports and the words it uses are both part of the
contract. A substring assertion still passes when a count is off by one or a
refusal loses the name of the file it refused, which is exactly what an
operator needs when a shard stalls among the 386.
"""

import hashlib
import json
from pathlib import Path

import pytest
from shapely.geometry import Polygon

from osm_polygon_description_tag.dataset.languages.checkpoint import (
    part_name_for_offset,
    shard_paths,
)
from osm_polygon_description_tag.dataset.languages.models import LanguageResult, LanguageStatus
from osm_polygon_description_tag.dataset.languages.snapshot import (
    SnapshotManifest,
    prepare_snapshot,
)
from osm_polygon_description_tag.dataset.languages.validation import validate_run
from osm_polygon_description_tag.dataset.languages.worker import process_shard
from osm_polygon_description_tag.storage import write_geoparquet
from tests.conftest import make_record_dict
from tests.helpers.sentences import fake_splitter

SHARD = "region.parquet"
_ROWS = 8
_BATCH = 4


def _detector(text: str) -> LanguageResult:
    return LanguageResult("eng", 0.9, 0.1, 0.8, LanguageStatus.DETECTED, "detected")


def _prepared(tmp_path: Path) -> tuple[Path, Path, SnapshotManifest]:
    source = tmp_path / "source"
    records = (
        make_record_dict(
            Polygon([(0, 0), (0, 1), (1, 1), (1, 0)]),
            {"description": f"A synthetic description number {index}"},
            osm_id=index + 1,
        )
        for index in range(_ROWS)
    )
    path = source / SHARD
    path.parent.mkdir(parents=True, exist_ok=True)
    write_geoparquet(records, path, batch_size=_BATCH)
    run = tmp_path / "run"
    snapshot = prepare_snapshot(source, run, code_fingerprint="a" * 64, lock_fingerprint="b" * 64)
    process_shard(
        run,
        source,
        SHARD,
        detector=_detector,
        splitter=fake_splitter(),
        snapshot=snapshot,
        batch_size=_BATCH,
    )
    return source, run, snapshot


def test_a_complete_run_reports_the_snapshot_identity_it_validated_against(
    tmp_path: Path,
) -> None:
    """The fingerprint names which detector configuration the report describes."""
    _, run, snapshot = _prepared(tmp_path)

    report = validate_run(run)

    assert report.snapshot_id == snapshot.snapshot_id
    assert report.model_config_fingerprint == snapshot.model_config_fingerprint


def test_an_unreadable_checkpoint_reports_zeroed_counts_for_its_own_shard(
    tmp_path: Path,
) -> None:
    """A shard whose checkpoint cannot be read has observed nothing, not one of anything."""
    _, run, _ = _prepared(tmp_path)
    shard_paths(run, SHARD).checkpoint.write_text("{not-json", encoding="utf-8")

    report = validate_run(run)
    shard = report.shards[0]

    assert shard.shard == SHARD
    assert shard.status == "unreadable"
    assert shard.input_row_count == _ROWS
    assert shard.input_cursor == 0
    assert shard.annotation_count == 0
    assert shard.part_count == 0
    assert len(shard.issues) == 1


@pytest.mark.parametrize("damage", ["missing", "symlinked", "not_parquet"])
def test_a_rejected_part_contributes_no_annotations_to_the_shard(
    tmp_path: Path, damage: str
) -> None:
    """A rejected part must count zero rows; counting one hides a lost batch."""
    _, run, _ = _prepared(tmp_path)
    part = shard_paths(run, SHARD).part(part_name_for_offset(0))
    if damage == "missing":
        part.unlink()
    elif damage == "symlinked":
        target = tmp_path / "moved-part.parquet"
        part.rename(target)
        part.symlink_to(target)
    else:
        receipt_path = shard_paths(run, SHARD).receipt(part_name_for_offset(0))
        receipt = json.loads(receipt_path.read_text(encoding="utf-8"))
        part.write_bytes(b"not parquet at all")
        receipt["part_sha256"] = hashlib.sha256(part.read_bytes()).hexdigest()
        receipt_path.write_text(json.dumps(receipt), encoding="utf-8")

    report = validate_run(run)

    assert not report.is_complete
    assert report.shards[0].annotation_count == _ROWS - _BATCH
    assert report.annotation_count == _ROWS - _BATCH


def test_a_rejected_receipt_contributes_no_annotations_to_the_shard(tmp_path: Path) -> None:
    """An unusable receipt leaves its part uncounted rather than counted as one row."""
    _, run, _ = _prepared(tmp_path)
    shard_paths(run, SHARD).receipt(part_name_for_offset(0)).write_text("[]", encoding="utf-8")

    report = validate_run(run)

    assert not report.is_complete
    assert report.shards[0].annotation_count == _ROWS - _BATCH


def test_a_symlinked_receipt_is_refused_rather_than_followed(tmp_path: Path) -> None:
    """A receipt outside the run must not be trusted even when it parses."""
    _, run, _ = _prepared(tmp_path)
    part_name = part_name_for_offset(0)
    receipt = shard_paths(run, SHARD).receipt(part_name)
    target = tmp_path / "external-receipt.json"
    receipt.rename(target)
    receipt.symlink_to(target)

    report = validate_run(run)

    assert not report.is_complete
    assert f"receipt is unusable for {part_name}: not a regular file" in report.shards[0].issues


@pytest.mark.parametrize(
    ("changes", "issue"),
    [
        (
            {"snapshot_id": "c" * 64},
            "checkpoint snapshot identity does not match the run snapshot",
        ),
        (
            {"model_config_fingerprint": "c" * 64},
            "checkpoint detector configuration does not match the run snapshot",
        ),
        (
            {"input_row_count": 99, "status": "paused"},
            "checkpoint input row count does not match the snapshot source file",
        ),
    ],
)
def test_a_checkpoint_identity_mismatch_is_reported_in_full(
    tmp_path: Path, changes: dict[str, object], issue: str
) -> None:
    """The whole sentence is the contract: it names which binding disagreed."""
    _, run, _ = _prepared(tmp_path)
    path = shard_paths(run, SHARD).checkpoint
    payload = json.loads(path.read_text(encoding="utf-8"))
    payload.update(changes)
    path.write_text(json.dumps(payload), encoding="utf-8")

    report = validate_run(run)

    assert issue in report.shards[0].issues
