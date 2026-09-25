"""Bounded, resumable, crash-safe shard processing."""

import hashlib
import json
from collections.abc import Iterator
from pathlib import Path

import pyarrow as pa
import pyarrow.parquet as pq
import pytest
from shapely.geometry import Polygon

from osm_polygon_description_tag.dataset.languages.annotations import (
    ANNOTATION_SCHEMA,
    AnnotationError,
    read_annotation_part,
)
from osm_polygon_description_tag.dataset.languages.checkpoint import (
    CheckpointError,
    ShardCheckpoint,
    ShardStatus,
    part_name_for_offset,
    read_checkpoint,
    shard_paths,
    write_checkpoint,
)
from osm_polygon_description_tag.dataset.languages.models import (
    LanguagePolicy,
    LanguageResult,
    LanguageStatus,
    language_model_identity,
)
from osm_polygon_description_tag.dataset.languages.snapshot import (
    SnapshotManifest,
    prepare_snapshot,
)
from osm_polygon_description_tag.dataset.languages.worker import (
    MAX_BATCH_SIZE,
    BoundedTextCache,
    ProcessingBudget,
    ShardOutcome,
    process_shard,
)
from osm_polygon_description_tag.storage import write_geoparquet
from tests.conftest import make_record_dict
from tests.helpers.messages import exactly
from tests.helpers.parquet import write_description_shard
from tests.helpers.sentences import fake_splitter

SHARD = "region.parquet"


def _detector(text: str) -> LanguageResult:
    """A deterministic stand-in that never loads a real language model."""
    stripped = text.strip()
    if not stripped:
        return LanguageResult(None, None, None, None, LanguageStatus.NON_LINGUISTIC, "empty")
    if stripped.startswith("?"):
        return LanguageResult(None, 0.5, 0.4, 0.5 - 0.4, LanguageStatus.UNCERTAIN, "low_confidence")
    code = "fra" if stripped.startswith("Le ") else "eng"
    return LanguageResult(code, 0.9, 0.1, 0.8, LanguageStatus.DETECTED, "detected")


class _CountingDetector:
    def __init__(self) -> None:
        self.calls = 0

    def __call__(self, text: str) -> LanguageResult:
        self.calls += 1
        return _detector(text)


def _tags_for(index: int) -> dict[str, str]:
    tags = {"description": f"A synthetic description number {index}"}
    if index % 3 == 0:
        tags["description:fr"] = f"Le batiment numero {index}"
    if index % 7 == 0:
        tags["description:xyz"] = "?"
    return tags


def _write_shard(path: Path, count: int, *, row_group_size: int = 4) -> None:
    write_description_shard(path, count, batch_size=row_group_size, tags=_tags_for)
    _set_row_group_size(path, row_group_size)


def _set_row_group_size(path: Path, row_group_size: int) -> None:
    """Re-lay a shard into fixed-size row groups, preserving its schema."""
    table = pq.read_table(path)
    pq.write_table(table, path, row_group_size=row_group_size, compression="zstd")


def _strip_description_tags(path: Path) -> None:
    """Rewrite a shard so no row carries a description tag at all."""
    table = pq.read_table(path)
    kept = [
        [pair for pair in row if not str(pair["key"]).startswith("description")]
        for row in table.column("tags").to_pylist()
    ]
    index = table.schema.get_field_index("tags")
    rewritten = table.set_column(
        index, table.schema.field(index), pa.array(kept, type=table.schema.field(index).type)
    )
    pq.write_table(rewritten, path)


def _prepare(
    tmp_path: Path, *, count: int = 12, row_group_size: int = 4
) -> tuple[Path, Path, SnapshotManifest]:
    source = tmp_path / "source"
    _write_shard(source / SHARD, count, row_group_size=row_group_size)
    run = tmp_path / "run"
    snapshot = prepare_snapshot(
        source,
        run,
        code_fingerprint="a" * 64,
        lock_fingerprint="b" * 64,
        model_identity=language_model_identity(LanguagePolicy()),
    )
    return source, run, snapshot


def _process(
    run: Path,
    source: Path,
    snapshot: SnapshotManifest,
    *,
    batch_size: int = 4,
    budget: ProcessingBudget | None = None,
    detector: object = None,
) -> ShardOutcome:
    return process_shard(
        run,
        source,
        SHARD,
        detector=detector or _detector,
        splitter=fake_splitter(),  # type: ignore[arg-type]
        snapshot=snapshot,
        batch_size=batch_size,
        budget=budget,
    )


@pytest.mark.parametrize("kind", ["dangling_symlink", "directory"])
def test_invalid_checkpoint_path_is_not_treated_as_a_new_run(tmp_path: Path, kind: str) -> None:
    source, run, snapshot = _prepare(tmp_path)
    path = shard_paths(run, SHARD).checkpoint
    path.parent.mkdir(parents=True)
    if kind == "directory":
        path.mkdir()
    else:
        path.symlink_to(tmp_path / "missing-external-checkpoint")
    detector = _CountingDetector()

    with pytest.raises(CheckpointError):
        _process(run, source, snapshot, detector=detector)
    assert detector.calls == 0
    assert path.is_dir() if kind == "directory" else path.is_symlink()


def _annotations(run: Path) -> list[dict[str, object]]:
    paths = shard_paths(run, SHARD)
    rows: list[dict[str, object]] = []
    for part in sorted(paths.parts.glob("part-*.parquet")):
        rows.extend(read_annotation_part(part).to_pylist())
    return rows


class _StepBudget(ProcessingBudget):
    """A budget that expires after a fixed number of committed batches."""

    def __init__(self, batches: int) -> None:
        self._remaining = batches
        super().__init__(60.0, clock=lambda: 0.0)

    def exhausted(self) -> bool:
        if self._remaining <= 0:
            return True
        self._remaining -= 1
        return False


def test_processing_annotates_every_description_value_once(tmp_path: Path) -> None:
    source, run, snapshot = _prepare(tmp_path)

    outcome = _process(run, source, snapshot)
    rows = _annotations(run)

    assert outcome.is_complete
    assert outcome.status is ShardStatus.COMPLETE
    assert outcome.input_cursor == outcome.input_row_count == 12
    assert outcome.annotation_count == len(rows)
    assert len({row["description_identity"] for row in rows}) == len(rows)
    assert sum(1 for row in rows if row["tag_key"] == "description") == 12
    assert sum(1 for row in rows if row["tag_key"] == "description:fr") == 4
    assert sum(1 for row in rows if row["tag_key"] == "description:xyz") == 2


def test_annotation_rows_carry_identity_provenance_and_run_binding(tmp_path: Path) -> None:
    source, run, snapshot = _prepare(tmp_path, count=4)

    _process(run, source, snapshot)
    rows = _annotations(run)
    base = next(row for row in rows if row["tag_key"] == "description")

    assert base["source_pbf"] == "region.osm.pbf"
    assert base["osm_type"] == "way"
    assert base["osm_id"] == 1
    assert base["original_text"] == "A synthetic description number 0"
    assert base["status"] == "detected"
    assert base["language_code"] == "eng"
    assert base["snapshot_id"] == snapshot.snapshot_id
    assert base["model_config_fingerprint"] == snapshot.model_config_fingerprint
    assert (
        read_annotation_part(shard_paths(run, SHARD).part(part_name_for_offset(0))).schema
        == ANNOTATION_SCHEMA
    )


def test_non_linguistic_and_uncertain_outcomes_are_preserved(tmp_path: Path) -> None:
    source, run, snapshot = _prepare(tmp_path, count=8)

    _process(run, source, snapshot)
    rows = _annotations(run)
    uncertain = [row for row in rows if row["status"] == "uncertain"]

    assert uncertain
    for row in uncertain:
        assert row["language_code"] is None
        assert row["reason"] == "low_confidence"


def test_an_empty_shard_completes_without_parts(tmp_path: Path) -> None:
    source, run, snapshot = _prepare(tmp_path, count=0)

    outcome = _process(run, source, snapshot)

    assert outcome.is_complete
    assert outcome.input_row_count == 0
    assert outcome.annotation_count == 0
    assert outcome.completed_parts == ()
    assert read_checkpoint(shard_paths(run, SHARD).checkpoint).is_complete


def test_rows_without_descriptions_still_advance_the_cursor(tmp_path: Path) -> None:
    source = tmp_path / "source"
    _write_shard(source / SHARD, 8, row_group_size=4)
    _strip_description_tags(source / SHARD)
    run = tmp_path / "run"
    snapshot = prepare_snapshot(source, run, code_fingerprint="a" * 64, lock_fingerprint="b" * 64)

    outcome = _process(run, source, snapshot)

    assert outcome.is_complete
    assert outcome.input_cursor == 8
    assert outcome.annotation_count == 0
    assert _annotations(run) == []
    assert len(outcome.completed_parts) == 2


def test_paused_and_resumed_output_matches_an_uninterrupted_run(tmp_path: Path) -> None:
    uninterrupted_source, uninterrupted_run, uninterrupted = _prepare(tmp_path / "one")
    _process(uninterrupted_run, uninterrupted_source, uninterrupted)
    expected = _annotations(uninterrupted_run)

    source, run, snapshot = _prepare(tmp_path / "two")
    first = _process(run, source, snapshot, budget=_StepBudget(1))
    assert not first.is_complete
    assert first.input_cursor == 4

    second = _process(run, source, snapshot, budget=_StepBudget(1))
    assert second.resumed_from == 4
    assert not second.is_complete

    final = _process(run, source, snapshot)

    assert final.is_complete
    assert final.input_cursor == 12
    assert _annotations(run) == expected
    assert final.annotation_count == len(expected)


def test_resuming_a_complete_shard_does_no_further_work(tmp_path: Path) -> None:
    source, run, snapshot = _prepare(tmp_path)
    first = _process(run, source, snapshot)

    detector = _CountingDetector()
    second = process_shard(
        run,
        source,
        SHARD,
        detector=detector,
        splitter=fake_splitter(),
        snapshot=snapshot,
        batch_size=4,
    )

    assert detector.calls == 0
    assert second.is_complete
    assert second.completed_parts == first.completed_parts
    assert second.annotation_count == first.annotation_count


def test_a_crash_before_the_checkpoint_commit_is_recovered(tmp_path: Path) -> None:
    source, run, snapshot = _prepare(tmp_path)
    reference_source, reference_run, reference = _prepare(tmp_path / "reference")
    _process(reference_run, reference_source, reference)
    expected = _annotations(reference_run)

    _process(run, source, snapshot, budget=_StepBudget(1))
    paths = shard_paths(run, SHARD)

    # Simulate a crash after the second part and receipt were committed but
    # before the checkpoint recorded them.
    orphan = part_name_for_offset(4)
    paths.parts.joinpath(orphan).write_bytes(paths.part(part_name_for_offset(0)).read_bytes())

    outcome = _process(run, source, snapshot)

    assert outcome.is_complete
    assert _annotations(run) == expected


def test_a_corrupt_committed_part_is_never_adopted(tmp_path: Path) -> None:
    source, run, snapshot = _prepare(tmp_path)
    _process(run, source, snapshot, budget=_StepBudget(1))
    paths = shard_paths(run, SHARD)
    paths.part(part_name_for_offset(0)).write_bytes(b"truncated")

    with pytest.raises(CheckpointError, match="does not match its receipt"):
        _process(run, source, snapshot)


def test_a_missing_committed_part_is_reported(tmp_path: Path) -> None:
    source, run, snapshot = _prepare(tmp_path)
    _process(run, source, snapshot, budget=_StepBudget(1))
    shard_paths(run, SHARD).part(part_name_for_offset(0)).unlink()

    with pytest.raises(CheckpointError, match="committed part is missing"):
        _process(run, source, snapshot)


def test_a_part_that_disagrees_with_its_receipt_row_count_is_reported(tmp_path: Path) -> None:
    source, run, snapshot = _prepare(tmp_path)
    _process(run, source, snapshot, budget=_StepBudget(1))
    paths = shard_paths(run, SHARD)
    receipt_path = paths.receipt(part_name_for_offset(0))
    payload = receipt_path.read_text(encoding="utf-8").replace(
        '"row_count":', '"row_count": 99,"_":'
    )
    receipt_path.write_text(payload, encoding="utf-8")

    with pytest.raises(CheckpointError):
        _process(run, source, snapshot)


def test_source_drift_is_rejected_before_any_processing(tmp_path: Path) -> None:
    source, run, snapshot = _prepare(tmp_path)
    _write_shard(source / SHARD, 16)

    with pytest.raises(Exception, match="does not match snapshot"):
        _process(run, source, snapshot)


def test_resuming_with_a_different_batch_size_is_rejected(tmp_path: Path) -> None:
    source, run, snapshot = _prepare(tmp_path)
    _process(run, source, snapshot, budget=_StepBudget(1))

    with pytest.raises(CheckpointError, match="written with batch size 4"):
        _process(run, source, snapshot, batch_size=8)


def test_an_initialized_checkpoint_without_parts_accepts_any_batch_size(tmp_path: Path) -> None:
    source, run, snapshot = _prepare(tmp_path)
    paths = shard_paths(run, SHARD)
    write_checkpoint(
        paths.checkpoint,
        ShardCheckpoint(
            snapshot_id=snapshot.snapshot_id,
            model_config_fingerprint=snapshot.model_config_fingerprint,
            shard=SHARD,
            batch_size=512,
            input_row_count=snapshot.source_file(SHARD).row_count,
            input_cursor=0,
            annotation_count=0,
            completed_parts=(),
            status=ShardStatus.PAUSED,
        ),
    )

    outcome = _process(run, source, snapshot, batch_size=4)

    assert outcome.status is ShardStatus.COMPLETE
    assert read_checkpoint(paths.checkpoint).batch_size == 4


def test_a_checkpoint_from_another_run_is_rejected(tmp_path: Path) -> None:
    source, run, snapshot = _prepare(tmp_path)
    _process(run, source, snapshot, budget=_StepBudget(1))

    # Same row count, different bytes, therefore a different snapshot identity.
    other_source, other_run, other = _prepare(tmp_path / "other", count=12, row_group_size=6)
    assert other.snapshot_id != snapshot.snapshot_id
    paths = shard_paths(run, SHARD)
    other_paths = shard_paths(other_run, SHARD)
    other_paths.checkpoint.parent.mkdir(parents=True, exist_ok=True)
    other_paths.checkpoint.write_bytes(paths.checkpoint.read_bytes())

    with pytest.raises(
        CheckpointError, match=exactly("checkpoint belongs to a different input snapshot")
    ):
        _process(other_run, other_source, other)


def test_only_the_selected_shard_must_be_staged(tmp_path: Path) -> None:
    source = tmp_path / "source"
    _write_shard(source / SHARD, 8)
    _write_shard(source / "other.parquet", 8)
    run = tmp_path / "run"
    snapshot = prepare_snapshot(source, run, code_fingerprint="a" * 64, lock_fingerprint="b" * 64)

    staged = tmp_path / "staged"
    staged.mkdir()
    staged.joinpath(SHARD).write_bytes((source / SHARD).read_bytes())

    outcome = process_shard(
        staged,
        staged,
        SHARD,
        detector=_detector,
        splitter=fake_splitter(),
        snapshot=snapshot,
        batch_size=4,
    )

    assert outcome.is_complete
    assert outcome.input_row_count == 8


def test_batches_are_bounded_and_projected(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    source, run, snapshot = _prepare(tmp_path, count=12, row_group_size=12)
    seen: list[tuple[int, list[str] | None]] = []
    original = pq.ParquetFile.iter_batches

    def _spy(self: pq.ParquetFile, **kwargs: object) -> Iterator[object]:
        seen.append((kwargs["batch_size"], kwargs.get("columns")))  # type: ignore[arg-type]
        return original(self, **kwargs)  # type: ignore[arg-type]

    monkeypatch.setattr(pq.ParquetFile, "iter_batches", _spy)
    outcome = _process(run, source, snapshot, batch_size=4)

    assert seen == [(4, ["source_pbf", "osm_type", "osm_id", "tags"])]
    assert len(outcome.completed_parts) == 3


def test_resume_skips_row_groups_already_committed(tmp_path: Path) -> None:
    source, run, snapshot = _prepare(tmp_path, count=12, row_group_size=4)
    _process(run, source, snapshot, budget=_StepBudget(2))

    detector = _CountingDetector()
    outcome = process_shard(
        run,
        source,
        SHARD,
        detector=detector,
        splitter=fake_splitter(),
        snapshot=snapshot,
        batch_size=4,
    )

    assert outcome.is_complete
    assert outcome.resumed_from == 8
    assert 0 < detector.calls <= 6


def test_a_misaligned_resume_cursor_is_rejected(tmp_path: Path) -> None:
    source, run, snapshot = _prepare(tmp_path, count=12, row_group_size=12)
    _process(run, source, snapshot, budget=_StepBudget(1))
    paths = shard_paths(run, SHARD)
    payload = paths.checkpoint.read_text(encoding="utf-8").replace(
        '"input_cursor":4', '"input_cursor":3'
    )
    paths.checkpoint.write_text(payload, encoding="utf-8")

    with pytest.raises(
        CheckpointError, match=exactly("checkpoint input cursor is not on a batch boundary")
    ):
        _process(run, source, snapshot)


def test_identical_text_reuses_inference_but_still_annotates_each_object(tmp_path: Path) -> None:
    source = tmp_path / "source"
    source.mkdir(parents=True)
    records = (
        make_record_dict(
            Polygon([(0, 0), (0, 1), (1, 1), (1, 0)]),
            {"description": "A shared description"},
            osm_id=index,
        )
        for index in range(1, 7)
    )
    write_geoparquet(records, source / SHARD, batch_size=6)
    run = tmp_path / "run"
    snapshot = prepare_snapshot(source, run, code_fingerprint="a" * 64, lock_fingerprint="b" * 64)

    detector = _CountingDetector()
    outcome = process_shard(
        run,
        source,
        SHARD,
        detector=detector,
        splitter=fake_splitter(),
        snapshot=snapshot,
        batch_size=6,
    )
    rows = _annotations(run)

    assert detector.calls == 1
    assert outcome.annotation_count == 6
    assert len({row["osm_id"] for row in rows}) == 6
    assert len({row["description_identity"] for row in rows}) == 6


def test_the_text_cache_is_bounded_and_reuses_recent_entries() -> None:
    cache = BoundedTextCache(max_entries=2)
    detector = _CountingDetector()

    cache.analysis_for("first text here", detector)
    cache.analysis_for("second text here", detector)
    cache.analysis_for("first text here", detector)
    assert detector.calls == 2
    assert len(cache) == 2

    cache.analysis_for("third text here", detector)
    assert len(cache) == 2
    cache.analysis_for("second text here", detector)
    assert detector.calls == 4


def test_the_text_cache_rejects_a_non_positive_bound() -> None:
    with pytest.raises(ValueError, match="positive integer"):
        BoundedTextCache(0)
    with pytest.raises(ValueError, match="positive integer"):
        BoundedTextCache(True)  # type: ignore[arg-type]


def test_a_finished_operation_at_the_deadline_has_expired() -> None:
    ticks = iter([100.0, 109.0, 110.0, 111.0])
    budget = ProcessingBudget(10.0, clock=lambda: next(ticks))
    budget.start()
    assert not budget.expired()
    assert budget.expired()
    assert budget.expired()


def test_the_budget_is_monotonic_and_validated() -> None:
    ticks = iter([100.0, 105.0, 110.0, 111.0])
    budget = ProcessingBudget(10.0, clock=lambda: next(ticks))
    assert budget.elapsed == 0.0
    budget.start()
    assert not budget.exhausted()
    assert budget.exhausted()
    assert budget.exhausted()

    with pytest.raises(ValueError, match="must be positive"):
        ProcessingBudget(0)
    with pytest.raises(TypeError, match=exactly("budget seconds must be a real number")):
        ProcessingBudget("10")  # type: ignore[arg-type]
    with pytest.raises(ValueError, match="finite"):
        ProcessingBudget(float("nan"))
    with pytest.raises(ValueError, match="finite"):
        ProcessingBudget(float("inf"))


def test_a_batch_that_finishes_after_the_budget_is_not_committed(tmp_path: Path) -> None:
    source, run, snapshot = _prepare(tmp_path)
    ticks = iter([0.0, 0.0, 2.0])
    budget = ProcessingBudget(1.0, clock=lambda: next(ticks))

    outcome = _process(run, source, snapshot, budget=budget)

    assert not outcome.is_complete
    assert outcome.input_cursor == 0
    assert outcome.completed_parts == ()


def test_an_exhausted_budget_pauses_before_the_first_batch(tmp_path: Path) -> None:
    source, run, snapshot = _prepare(tmp_path)

    outcome = _process(run, source, snapshot, budget=_StepBudget(0))

    assert not outcome.is_complete
    assert outcome.input_cursor == 0
    assert outcome.completed_parts == ()
    assert read_checkpoint(shard_paths(run, SHARD).checkpoint).input_cursor == 0


def test_detector_errors_are_not_converted_into_completed_results(tmp_path: Path) -> None:
    source, run, snapshot = _prepare(tmp_path)

    def _failing(text: str) -> LanguageResult:
        raise RuntimeError("detector exploded")

    with pytest.raises(RuntimeError, match="detector exploded"):
        process_shard(
            run,
            source,
            SHARD,
            detector=_failing,
            splitter=fake_splitter(),
            snapshot=snapshot,
            batch_size=4,
        )

    assert not shard_paths(run, SHARD).checkpoint.exists()


def test_an_invalid_batch_size_is_rejected(tmp_path: Path) -> None:
    source, run, snapshot = _prepare(tmp_path)

    with pytest.raises(ValueError, match=exactly("batch_size must be a positive integer")):
        _process(run, source, snapshot, batch_size=0)

    with pytest.raises(ValueError, match="batch_size must not exceed"):
        _process(run, source, snapshot, batch_size=MAX_BATCH_SIZE + 1)


def test_an_unknown_shard_is_rejected(tmp_path: Path) -> None:
    source, run, snapshot = _prepare(tmp_path)

    with pytest.raises(Exception, match="not in snapshot"):
        process_shard(
            run,
            source,
            "absent.parquet",
            detector=_detector,
            splitter=fake_splitter(),
            snapshot=snapshot,
        )


def _tamper_checkpoint(run: Path, **changes: object) -> None:
    path = shard_paths(run, SHARD).checkpoint
    payload = json.loads(path.read_text(encoding="utf-8"))
    payload.update(changes)
    path.write_text(json.dumps(payload), encoding="utf-8")


@pytest.mark.parametrize(
    ("changes", "message"),
    [
        ({"model_config_fingerprint": "c" * 64}, "different detector configuration"),
        ({"input_row_count": 99}, "input row count does not match the source file"),
    ],
)
def test_a_checkpoint_that_does_not_match_the_run_is_rejected(
    tmp_path: Path, changes: dict[str, object], message: str
) -> None:
    source, run, snapshot = _prepare(tmp_path)
    _process(run, source, snapshot, budget=_StepBudget(1))
    _tamper_checkpoint(run, **changes)

    with pytest.raises(CheckpointError, match=message):
        _process(run, source, snapshot)


def test_a_checkpoint_recorded_against_another_shard_is_rejected(tmp_path: Path) -> None:
    source = tmp_path / "source"
    _write_shard(source / SHARD, 12)
    _write_shard(source / "other.parquet", 12)
    run = tmp_path / "run"
    snapshot = prepare_snapshot(source, run, code_fingerprint="a" * 64, lock_fingerprint="b" * 64)
    _process(run, source, snapshot, budget=_StepBudget(1))
    _tamper_checkpoint(run, shard="other.parquet")

    with pytest.raises(CheckpointError, match=exactly("checkpoint belongs to a different shard")):
        _process(run, source, snapshot)


def test_a_receipt_from_another_run_is_rejected(tmp_path: Path) -> None:
    source, run, snapshot = _prepare(tmp_path)
    _process(run, source, snapshot, budget=_StepBudget(1))
    receipt_path = shard_paths(run, SHARD).receipt(part_name_for_offset(0))
    payload = json.loads(receipt_path.read_text(encoding="utf-8"))
    payload["snapshot_id"] = "c" * 64
    receipt_path.write_text(json.dumps(payload), encoding="utf-8")

    with pytest.raises(CheckpointError, match="receipt does not belong to this run"):
        _process(run, source, snapshot)


def test_a_part_that_hashes_correctly_but_is_not_parquet_is_rejected(tmp_path: Path) -> None:
    source, run, snapshot = _prepare(tmp_path)
    _process(run, source, snapshot, budget=_StepBudget(1))
    paths = shard_paths(run, SHARD)
    part_name = part_name_for_offset(0)
    corrupt = b"this is not parquet"
    paths.part(part_name).write_bytes(corrupt)
    receipt_path = paths.receipt(part_name)
    payload = json.loads(receipt_path.read_text(encoding="utf-8"))
    payload["part_sha256"] = hashlib.sha256(corrupt).hexdigest()
    receipt_path.write_text(json.dumps(payload), encoding="utf-8")

    with pytest.raises(CheckpointError, match="committed part is unreadable"):
        _process(run, source, snapshot)


@pytest.mark.parametrize(
    ("field", "value", "message"),
    [
        ("model_config_fingerprint", "c" * 64, "model fingerprint"),
        ("source_sha256", "c" * 64, "receipt source does not match snapshot"),
        ("source_schema_fingerprint", "c" * 64, "receipt source schema does not match snapshot"),
    ],
)
def test_a_receipt_with_a_foreign_binding_is_rejected(
    tmp_path: Path, field: str, value: str, message: str
) -> None:
    source, run, snapshot = _prepare(tmp_path)
    _process(run, source, snapshot, budget=_StepBudget(1))
    receipt_path = shard_paths(run, SHARD).receipt(part_name_for_offset(0))
    payload = json.loads(receipt_path.read_text(encoding="utf-8"))
    payload[field] = value
    receipt_path.write_text(json.dumps(payload), encoding="utf-8")

    with pytest.raises(CheckpointError, match=message):
        _process(run, source, snapshot)


def test_a_receipt_with_a_foreign_part_name_is_rejected(tmp_path: Path) -> None:
    source, run, snapshot = _prepare(tmp_path)
    _process(run, source, snapshot, budget=_StepBudget(1))
    receipt_path = shard_paths(run, SHARD).receipt(part_name_for_offset(0))
    payload = json.loads(receipt_path.read_text(encoding="utf-8"))
    payload.update(
        {
            "part_name": part_name_for_offset(4),
            "input_row_start": 4,
            "input_row_end": 8,
        }
    )
    receipt_path.write_text(json.dumps(payload), encoding="utf-8")

    with pytest.raises(CheckpointError, match="part name does not match"):
        _process(run, source, snapshot)


def test_a_part_whose_rows_disagree_with_its_receipt_is_rejected(tmp_path: Path) -> None:
    source, run, snapshot = _prepare(tmp_path)
    _process(run, source, snapshot, budget=_StepBudget(1))
    paths = shard_paths(run, SHARD)
    part_name = part_name_for_offset(0)
    receipt_path = paths.receipt(part_name)
    payload = json.loads(receipt_path.read_text(encoding="utf-8"))
    payload["row_count"] = payload["row_count"] + 1
    receipt_path.write_text(json.dumps(payload), encoding="utf-8")

    with pytest.raises(CheckpointError, match="row count does not match its receipt"):
        _process(run, source, snapshot)


def test_a_checkpoint_whose_total_disagrees_with_its_parts_is_rejected(tmp_path: Path) -> None:
    source, run, snapshot = _prepare(tmp_path)
    _process(run, source, snapshot, budget=_StepBudget(1))
    _tamper_checkpoint(run, annotation_count=999)

    with pytest.raises(
        CheckpointError,
        match=exactly("committed parts do not account for the recorded annotations"),
    ):
        _process(run, source, snapshot)


def test_resume_skips_earlier_batches_inside_one_row_group(tmp_path: Path) -> None:
    source, run, snapshot = _prepare(tmp_path, count=12, row_group_size=12)
    _process(run, source, snapshot, budget=_StepBudget(2))
    reference_source, reference_run, reference = _prepare(tmp_path / "reference", row_group_size=12)
    _process(reference_run, reference_source, reference)

    outcome = _process(run, source, snapshot)

    assert outcome.resumed_from == 8
    assert outcome.is_complete
    assert _annotations(run) == _annotations(reference_run)


def test_resume_preserves_the_global_batch_grid_across_row_groups(tmp_path: Path) -> None:
    reference_source, reference_run, reference = _prepare(
        tmp_path / "reference", count=18, row_group_size=6
    )
    _process(reference_run, reference_source, reference, batch_size=4)

    source, run, snapshot = _prepare(tmp_path / "resumed", count=18, row_group_size=6)
    first = _process(run, source, snapshot, batch_size=4, budget=_StepBudget(2))
    assert not first.is_complete

    resumed = _process(run, source, snapshot, batch_size=4)

    assert resumed.is_complete
    assert _annotations(run) == _annotations(reference_run)


def test_resume_rejects_a_gap_in_the_committed_receipt_chain(tmp_path: Path) -> None:
    source, run, snapshot = _prepare(tmp_path, count=12, row_group_size=4)
    _process(run, source, snapshot, batch_size=4)
    paths = shard_paths(run, SHARD)
    first = part_name_for_offset(0)
    receipt_path = paths.receipt(first)
    payload = json.loads(receipt_path.read_text(encoding="utf-8"))
    payload["input_row_end"] = 3
    receipt_path.write_text(json.dumps(payload), encoding="utf-8")

    with pytest.raises(CheckpointError, match="contiguous"):
        _process(run, source, snapshot, batch_size=4)


def test_processing_rejects_an_identity_duplicate_across_batches(tmp_path: Path) -> None:
    source = tmp_path / "source"
    source.mkdir(parents=True)
    _write_shard(source / SHARD, 8, row_group_size=4)
    table = pq.read_table(source / SHARD)
    duplicated = pa.concat_tables([table.slice(0, 4), table.slice(0, 1), table.slice(5, 3)])
    pq.write_table(duplicated, source / SHARD, row_group_size=4, compression="zstd")
    run = tmp_path / "run"
    snapshot = prepare_snapshot(source, run, code_fingerprint="a" * 64, lock_fingerprint="b" * 64)

    with pytest.raises(AnnotationError, match="duplicate description identity"):
        _process(run, source, snapshot, batch_size=4)

    checkpoint = read_checkpoint(shard_paths(run, SHARD).checkpoint)
    assert checkpoint.input_cursor == 4
