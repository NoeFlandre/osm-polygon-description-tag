"""Read-only completeness validation of a local language run."""

import hashlib
import json
from pathlib import Path

import pyarrow as pa
import pyarrow.parquet as pq
import pytest
from shapely.geometry import Polygon

from osm_polygon_description_tag.dataset.languages.annotations import read_annotation_part
from osm_polygon_description_tag.dataset.languages.checkpoint import (
    part_name_for_offset,
    receipt_name_for_part,
    shard_paths,
)
from osm_polygon_description_tag.dataset.languages.models import (
    LanguageResult,
    LanguageStatus,
)
from osm_polygon_description_tag.dataset.languages.snapshot import (
    SnapshotError,
    SnapshotManifest,
    prepare_snapshot,
)
from osm_polygon_description_tag.dataset.languages.validation import validate_run
from osm_polygon_description_tag.dataset.languages.worker import process_shard
from osm_polygon_description_tag.storage import write_geoparquet
from tests.conftest import make_record_dict

SHARD = "region.parquet"
OTHER = "other.parquet"


def _detector(text: str) -> LanguageResult:
    return LanguageResult("eng", 0.9, 0.1, 0.8, LanguageStatus.DETECTED, "detected")


def _write_shard(path: Path, count: int, *, start: int = 0) -> None:
    records = (
        make_record_dict(
            Polygon([(0, 0), (0, 1), (1, 1), (1, 0)]),
            {"description": f"A synthetic description number {index}"},
            osm_id=index + 1,
        )
        for index in range(start, start + count)
    )
    path.parent.mkdir(parents=True, exist_ok=True)
    write_geoparquet(records, path, batch_size=4)


def _prepare(
    tmp_path: Path, *, shards: tuple[str, ...] = (SHARD,)
) -> tuple[Path, Path, SnapshotManifest]:
    source = tmp_path / "source"
    for index, name in enumerate(shards):
        _write_shard(source / name, 8, start=index * 100)
    run = tmp_path / "run"
    snapshot = prepare_snapshot(source, run, code_fingerprint="a" * 64, lock_fingerprint="b" * 64)
    return source, run, snapshot


def _process(run: Path, source: Path, snapshot: SnapshotManifest, shard: str = SHARD) -> None:
    process_shard(run, source, shard, detector=_detector, snapshot=snapshot, batch_size=4)


def _listing(root: Path) -> list[str]:
    return sorted(str(path.relative_to(root)) for path in root.rglob("*"))


def test_a_finished_run_validates_as_complete(tmp_path: Path) -> None:
    source, run, snapshot = _prepare(tmp_path)
    _process(run, source, snapshot)

    report = validate_run(run)

    assert report.is_complete
    assert report.shard_count == 1
    assert report.complete_shard_count == 1
    assert report.annotation_count == 8
    assert report.input_row_count == 8
    assert report.issues == ()
    assert report.snapshot_id == snapshot.snapshot_id
    assert report.shards[0].part_count == 2
    assert report.shards[0].issues == ()


def test_validation_never_modifies_the_run_directory(tmp_path: Path) -> None:
    source, run, snapshot = _prepare(tmp_path)
    _process(run, source, snapshot)
    before = _listing(run)

    validate_run(run)

    assert _listing(run) == before


def test_an_unprocessed_shard_is_reported_as_missing(tmp_path: Path) -> None:
    source, run, snapshot = _prepare(tmp_path, shards=(SHARD, OTHER))
    _process(run, source, snapshot, SHARD)

    report = validate_run(run)
    other = next(item for item in report.shards if item.shard == OTHER)

    assert not report.is_complete
    assert report.shard_count == 2
    assert report.complete_shard_count == 1
    assert other.status == "missing"
    assert other.input_row_count == 8
    assert other.input_cursor == 0
    assert other.annotation_count == 0
    assert other.part_count == 0
    assert other.issues == ("no checkpoint has been written for this shard",)


@pytest.mark.parametrize("kind", ["dangling_symlink", "directory"])
def test_invalid_checkpoint_path_is_reported_as_unreadable(tmp_path: Path, kind: str) -> None:
    _, run, _ = _prepare(tmp_path)
    path = shard_paths(run, SHARD).checkpoint
    path.parent.mkdir(parents=True)
    if kind == "directory":
        path.mkdir()
    else:
        path.symlink_to(tmp_path / "missing-external-checkpoint")
    before = _listing(run)

    report = validate_run(run)

    assert not report.is_complete
    assert report.shards[0].status == "unreadable"
    assert report.shards[0].issues
    assert _listing(run) == before


def test_validation_can_be_restricted_to_one_shard(tmp_path: Path) -> None:
    source, run, snapshot = _prepare(tmp_path, shards=(SHARD, OTHER))
    _process(run, source, snapshot, SHARD)

    report = validate_run(run, shards=(SHARD,))

    assert report.is_complete
    assert report.shard_count == 1
    assert report.shards[0].shard == SHARD


def test_validation_rejects_an_unknown_restricted_shard(tmp_path: Path) -> None:
    source, run, snapshot = _prepare(tmp_path)
    _process(run, source, snapshot)

    with pytest.raises(SnapshotError, match="not in snapshot"):
        validate_run(run, shards=("absent.parquet",))


def test_a_part_that_no_longer_matches_its_receipt_is_reported(tmp_path: Path) -> None:
    source, run, snapshot = _prepare(tmp_path)
    _process(run, source, snapshot)
    shard_paths(run, SHARD).part(part_name_for_offset(0)).write_bytes(b"corrupted")

    report = validate_run(run)

    assert not report.is_complete
    assert any("does not match its receipt hash" in issue for issue in report.shards[0].issues)


def test_a_missing_part_is_reported(tmp_path: Path) -> None:
    source, run, snapshot = _prepare(tmp_path)
    _process(run, source, snapshot)
    shard_paths(run, SHARD).part(part_name_for_offset(0)).unlink()

    report = validate_run(run)

    assert not report.is_complete
    assert any("committed part is missing" in issue for issue in report.shards[0].issues)


def test_a_malformed_receipt_is_reported_without_modifying_it(tmp_path: Path) -> None:
    source, run, snapshot = _prepare(tmp_path)
    _process(run, source, snapshot)
    part_name = part_name_for_offset(0)
    receipt = shard_paths(run, SHARD).receipt(part_name)
    receipt.write_text("[]", encoding="utf-8")

    report = validate_run(run)

    assert not report.is_complete
    assert (
        f"receipt is unusable for {part_name}: receipt payload must be an object: {receipt}"
        in report.shards[0].issues
    )
    assert receipt.read_text(encoding="utf-8") == "[]"


def test_a_symlinked_committed_part_is_reported_without_following_it(tmp_path: Path) -> None:
    source, run, snapshot = _prepare(tmp_path)
    _process(run, source, snapshot)
    part_name = part_name_for_offset(0)
    part = shard_paths(run, SHARD).part(part_name)
    target = tmp_path / "moved-part.parquet"
    part.rename(target)
    original = target.read_bytes()
    part.symlink_to(target)

    report = validate_run(run)

    assert not report.is_complete
    assert f"committed part is not a regular file: {part_name}" in report.shards[0].issues
    assert part.is_symlink()
    assert target.read_bytes() == original


def test_a_missing_receipt_is_reported(tmp_path: Path) -> None:
    source, run, snapshot = _prepare(tmp_path)
    _process(run, source, snapshot)
    shard_paths(run, SHARD).receipt(part_name_for_offset(0)).unlink()

    report = validate_run(run)

    assert not report.is_complete
    assert any("receipt is unusable" in issue for issue in report.shards[0].issues)


def test_unexpected_parts_and_receipts_are_reported(tmp_path: Path) -> None:
    source, run, snapshot = _prepare(tmp_path)
    _process(run, source, snapshot)
    paths = shard_paths(run, SHARD)
    stray = part_name_for_offset(64)
    paths.parts.joinpath(stray).write_bytes(b"stray")
    paths.receipts.joinpath(receipt_name_for_part(stray)).write_text("{}", encoding="utf-8")

    report = validate_run(run)
    issues = report.shards[0].issues

    assert not report.is_complete
    assert any("unexpected part file" in issue for issue in issues)
    assert any("unexpected receipt file" in issue for issue in issues)


def test_a_noncanonical_part_artifact_is_reported(tmp_path: Path) -> None:
    source, run, snapshot = _prepare(tmp_path)
    _process(run, source, snapshot)
    paths = shard_paths(run, SHARD)
    paths.parts.joinpath("junk.parquet").write_bytes(b"stale artifact")

    report = validate_run(run)

    assert not report.is_complete
    assert any("unexpected part file" in issue for issue in report.shards[0].issues)


def test_an_unexpected_shard_directory_is_reported(tmp_path: Path) -> None:
    source, run, snapshot = _prepare(tmp_path)
    _process(run, source, snapshot)
    (run / "shards" / ("0" * 32)).mkdir(parents=True)

    report = validate_run(run)

    assert not report.is_complete
    assert any("unexpected shard directory" in issue for issue in report.issues)


def test_an_unreadable_checkpoint_is_reported(tmp_path: Path) -> None:
    source, run, snapshot = _prepare(tmp_path)
    _process(run, source, snapshot)
    shard_paths(run, SHARD).checkpoint.write_text("{not-json", encoding="utf-8")

    report = validate_run(run)

    assert not report.is_complete
    assert report.shards[0].status == "unreadable"
    assert report.shards[0].issues


def test_a_checkpoint_from_another_configuration_is_reported(tmp_path: Path) -> None:
    source, run, snapshot = _prepare(tmp_path)
    _process(run, source, snapshot)
    path = shard_paths(run, SHARD).checkpoint
    payload = json.loads(path.read_text(encoding="utf-8"))
    payload["model_config_fingerprint"] = "c" * 64
    path.write_text(json.dumps(payload), encoding="utf-8")

    report = validate_run(run)

    assert not report.is_complete
    assert any(
        "detector configuration does not match" in issue for issue in report.shards[0].issues
    )


def test_a_tampered_annotation_count_is_reported(tmp_path: Path) -> None:
    source, run, snapshot = _prepare(tmp_path)
    _process(run, source, snapshot)
    path = shard_paths(run, SHARD).checkpoint
    payload = json.loads(path.read_text(encoding="utf-8"))
    payload["annotation_count"] = 99
    path.write_text(json.dumps(payload), encoding="utf-8")

    report = validate_run(run)

    assert not report.is_complete
    assert any("but the checkpoint records 99" in issue for issue in report.shards[0].issues)


def test_a_paused_run_is_reported_as_incomplete(tmp_path: Path) -> None:
    source, run, snapshot = _prepare(tmp_path)
    path = shard_paths(run, SHARD).checkpoint
    _process(run, source, snapshot)
    payload = json.loads(path.read_text(encoding="utf-8"))
    payload["status"] = "paused"
    payload["input_cursor"] = 4
    payload["completed_parts"] = [part_name_for_offset(0)]
    payload["annotation_count"] = 4
    path.write_text(json.dumps(payload), encoding="utf-8")

    report = validate_run(run)

    assert not report.is_complete
    assert report.shards[0].status == "paused"
    assert report.shards[0].input_cursor == 4


def test_the_report_payload_is_json_serializable(tmp_path: Path) -> None:
    source, run, snapshot = _prepare(tmp_path)
    _process(run, source, snapshot)

    payload = validate_run(run).to_payload()

    assert json.loads(json.dumps(payload))["complete"] is True
    assert payload["shards"][0]["shard"] == SHARD


def _tamper(run: Path, **changes: object) -> None:
    path = shard_paths(run, SHARD).checkpoint
    payload = json.loads(path.read_text(encoding="utf-8"))
    payload.update(changes)
    path.write_text(json.dumps(payload), encoding="utf-8")


@pytest.mark.parametrize(
    ("changes", "message"),
    [
        ({"snapshot_id": "c" * 64}, "snapshot identity does not match"),
        (
            {"input_row_count": 99, "status": "paused"},
            "input row count does not match",
        ),
    ],
)
def test_checkpoint_identity_mismatches_are_reported(
    tmp_path: Path, changes: dict[str, object], message: str
) -> None:
    source, run, snapshot = _prepare(tmp_path)
    _process(run, source, snapshot)
    _tamper(run, **changes)

    report = validate_run(run)

    assert not report.is_complete
    assert any(message in issue for issue in report.shards[0].issues)


def test_a_checkpoint_recorded_against_another_shard_is_reported(tmp_path: Path) -> None:
    source, run, snapshot = _prepare(tmp_path, shards=(SHARD, OTHER))
    _process(run, source, snapshot, SHARD)
    _tamper(run, shard=OTHER)

    report = validate_run(run, shards=(SHARD,))

    assert any("records a different shard" in issue for issue in report.shards[0].issues)


def test_a_receipt_from_another_run_is_reported(tmp_path: Path) -> None:
    source, run, snapshot = _prepare(tmp_path)
    _process(run, source, snapshot)
    receipt_path = shard_paths(run, SHARD).receipt(part_name_for_offset(0))
    payload = json.loads(receipt_path.read_text(encoding="utf-8"))
    payload["snapshot_id"] = "c" * 64
    receipt_path.write_text(json.dumps(payload), encoding="utf-8")

    report = validate_run(run)

    assert any("receipt belongs to another run" in issue for issue in report.shards[0].issues)


def test_a_part_that_hashes_correctly_but_is_not_parquet_is_reported(tmp_path: Path) -> None:
    source, run, snapshot = _prepare(tmp_path)
    _process(run, source, snapshot)
    paths = shard_paths(run, SHARD)
    part_name = part_name_for_offset(0)
    corrupt = b"not parquet"
    paths.part(part_name).write_bytes(corrupt)
    receipt_path = paths.receipt(part_name)
    payload = json.loads(receipt_path.read_text(encoding="utf-8"))
    payload["part_sha256"] = hashlib.sha256(corrupt).hexdigest()
    receipt_path.write_text(json.dumps(payload), encoding="utf-8")

    report = validate_run(run)

    assert any("committed part is unreadable" in issue for issue in report.shards[0].issues)


def test_a_part_whose_rows_disagree_with_its_receipt_is_reported(tmp_path: Path) -> None:
    source, run, snapshot = _prepare(tmp_path)
    _process(run, source, snapshot)
    receipt_path = shard_paths(run, SHARD).receipt(part_name_for_offset(0))
    payload = json.loads(receipt_path.read_text(encoding="utf-8"))
    payload["row_count"] = payload["row_count"] + 1
    receipt_path.write_text(json.dumps(payload), encoding="utf-8")

    report = validate_run(run)

    assert any("row count does not match its receipt" in issue for issue in report.shards[0].issues)


def test_a_semantically_tampered_annotation_row_is_reported(tmp_path: Path) -> None:
    source, run, snapshot = _prepare(tmp_path)
    _process(run, source, snapshot)
    paths = shard_paths(run, SHARD)
    part_name = part_name_for_offset(0)
    part_path = paths.part(part_name)
    table = read_annotation_part(part_path)
    altered = table.set_column(
        table.schema.get_field_index("description_identity"),
        table.schema.field("description_identity"),
        pa.array(["c" * 64, *table.column("description_identity").to_pylist()[1:]]),
    )
    pq.write_table(altered, part_path, compression="zstd")
    receipt_path = paths.receipt(part_name)
    receipt = json.loads(receipt_path.read_text(encoding="utf-8"))
    receipt["part_sha256"] = hashlib.sha256(part_path.read_bytes()).hexdigest()
    receipt_path.write_text(json.dumps(receipt), encoding="utf-8")

    report = validate_run(run)

    assert not report.is_complete
    assert any("invalid description_identity" in issue for issue in report.shards[0].issues)


def test_a_receipt_with_a_foreign_source_binding_is_reported(tmp_path: Path) -> None:
    source, run, snapshot = _prepare(tmp_path)
    _process(run, source, snapshot)
    receipt_path = shard_paths(run, SHARD).receipt(part_name_for_offset(0))
    payload = json.loads(receipt_path.read_text(encoding="utf-8"))
    payload["source_sha256"] = "c" * 64
    receipt_path.write_text(json.dumps(payload), encoding="utf-8")

    report = validate_run(run)

    assert not report.is_complete
    assert any(
        "receipt source does not match snapshot" in issue for issue in report.shards[0].issues
    )


def test_a_checkpoint_with_a_gap_in_its_receipt_chain_is_reported(tmp_path: Path) -> None:
    source = tmp_path / "source"
    _write_shard(source / SHARD, 12)
    run = tmp_path / "run"
    snapshot = prepare_snapshot(source, run, code_fingerprint="a" * 64, lock_fingerprint="b" * 64)
    _process(run, source, snapshot)
    paths = shard_paths(run, SHARD)
    first = part_name_for_offset(0)
    receipt_path = paths.receipt(first)
    payload = json.loads(receipt_path.read_text(encoding="utf-8"))
    payload["input_row_end"] = 3
    receipt_path.write_text(json.dumps(payload), encoding="utf-8")

    report = validate_run(run)

    assert not report.is_complete
    assert any("contiguous" in issue for issue in report.shards[0].issues)


def test_validation_rejects_duplicate_description_identities_across_shards(
    tmp_path: Path,
) -> None:
    source = tmp_path / "source"
    _write_shard(source / SHARD, 4, start=0)
    _write_shard(source / OTHER, 4, start=0)
    run = tmp_path / "run"
    snapshot = prepare_snapshot(source, run, code_fingerprint="a" * 64, lock_fingerprint="b" * 64)
    _process(run, source, snapshot, SHARD)
    _process(run, source, snapshot, OTHER)

    report = validate_run(run)

    assert not report.is_complete
    assert any(
        "duplicate description identity" in issue
        for shard_report in report.shards
        for issue in shard_report.issues
    )


def test_rejected_part_does_not_count_rows_or_reserve_identities_for_later_shards(
    tmp_path: Path,
) -> None:
    source = tmp_path / "source"
    for shard in (OTHER, SHARD):
        _write_shard(source / shard, 4, start=0)
    run = tmp_path / "run"
    snapshot = prepare_snapshot(source, run, code_fingerprint="a" * 64, lock_fingerprint="b" * 64)
    for shard in (OTHER, SHARD):
        _process(run, source, snapshot, shard)
    part = part_name_for_offset(0)
    receipt_path = shard_paths(run, OTHER).receipt(part)
    payload = json.loads(receipt_path.read_text(encoding="utf-8"))
    payload["row_count"] = 5
    receipt_path.write_text(json.dumps(payload), encoding="utf-8")
    before = receipt_path.read_bytes()

    report = validate_run(run)

    assert [item.shard for item in report.shards] == [OTHER, SHARD]
    rejected, accepted = report.shards
    assert f"committed part row count does not match its receipt: {part}" in rejected.issues
    assert rejected.annotation_count == 0
    assert accepted.issues == ()
    assert accepted.annotation_count == 4
    assert report.annotation_count == 4
    assert not report.is_complete
    assert receipt_path.read_bytes() == before


def test_an_empty_shard_validates_without_any_part_directories(tmp_path: Path) -> None:
    source = tmp_path / "source"
    _write_shard(source / SHARD, 0)
    run = tmp_path / "run"
    snapshot = prepare_snapshot(source, run, code_fingerprint="a" * 64, lock_fingerprint="b" * 64)
    _process(run, source, snapshot)

    report = validate_run(run)

    assert report.is_complete
    assert report.annotation_count == 0
    assert report.shards[0].part_count == 0
    assert not shard_paths(run, SHARD).parts.exists()
