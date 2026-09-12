"""Durability and identity contracts of the shard worker's commit path.

A Grid'5000 shard is processed once and resumed from disk, so two properties
decide whether a restart is safe: an identity is reserved only after the part
that carries it is durably on disk, and a resumed run consults the identities
it already committed. Both are invisible to a test that only checks the happy
path, and both would corrupt a 386-shard run silently.
"""

import json
from dataclasses import replace
from pathlib import Path

import pytest
from shapely.geometry import Polygon

from osm_polygon_description_tag.dataset.languages import worker as worker_module
from osm_polygon_description_tag.dataset.languages.checkpoint import (
    CheckpointError,
    part_name_for_offset,
    read_checkpoint,
    shard_paths,
)
from osm_polygon_description_tag.dataset.languages.models import LanguageResult, LanguageStatus
from osm_polygon_description_tag.dataset.languages.snapshot import (
    SnapshotManifest,
    prepare_snapshot,
)
from osm_polygon_description_tag.dataset.languages.worker import MAX_BATCH_SIZE, process_shard
from osm_polygon_description_tag.storage import write_geoparquet
from tests.conftest import make_record_dict
from tests.helpers.messages import exactly
from tests.helpers.sentences import fake_splitter

SHARD = "region.parquet"
OTHER = "other.parquet"
_ROWS = 8
_BATCH = 4


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
    write_geoparquet(records, path, batch_size=_BATCH)


def _prepare(
    tmp_path: Path, *, shards: tuple[str, ...] = (SHARD,)
) -> tuple[Path, Path, SnapshotManifest]:
    source = tmp_path / "source"
    for index, name in enumerate(shards):
        _write_shard(source / name, _ROWS, start=index * 100)
    run = tmp_path / "run"
    snapshot = prepare_snapshot(source, run, code_fingerprint="a" * 64, lock_fingerprint="b" * 64)
    return source, run, snapshot


def test_a_finished_shard_reports_the_shard_it_processed_and_where_it_resumed(
    tmp_path: Path,
) -> None:
    """The outcome is the operator's record of which shard ran and from where."""
    source, run, snapshot = _prepare(tmp_path)

    outcome = process_shard(
        run,
        source,
        SHARD,
        detector=_detector,
        splitter=fake_splitter(),
        snapshot=snapshot,
        batch_size=_BATCH,
    )

    assert outcome.shard == SHARD
    assert outcome.resumed_from == 0
    assert outcome.input_cursor == _ROWS
    assert outcome.annotation_count == _ROWS


def test_reprocessing_a_complete_shard_reports_it_without_rewriting_anything(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A finished shard must be recognised from its checkpoint, not redone."""
    source, run, snapshot = _prepare(tmp_path)
    process_shard(
        run,
        source,
        SHARD,
        detector=_detector,
        splitter=fake_splitter(),
        snapshot=snapshot,
        batch_size=_BATCH,
    )

    def refuse(*_args: object, **_kwargs: object) -> None:
        raise AssertionError("a complete shard must not be written again")

    monkeypatch.setattr(worker_module, "write_checkpoint", refuse)

    outcome = process_shard(
        run,
        source,
        SHARD,
        detector=_detector,
        splitter=fake_splitter(),
        snapshot=snapshot,
        batch_size=_BATCH,
    )

    assert outcome.shard == SHARD
    assert outcome.resumed_from == _ROWS
    assert outcome.input_cursor == _ROWS
    assert outcome.annotation_count == _ROWS


def test_the_largest_documented_batch_size_is_accepted(tmp_path: Path) -> None:
    """``MAX_BATCH_SIZE`` is the documented maximum, so it must be usable."""
    source, run, snapshot = _prepare(tmp_path)

    outcome = process_shard(
        run,
        source,
        SHARD,
        detector=_detector,
        splitter=fake_splitter(),
        snapshot=snapshot,
        batch_size=MAX_BATCH_SIZE,
    )

    assert outcome.annotation_count == _ROWS
    assert read_checkpoint(shard_paths(run, SHARD).checkpoint).batch_size == MAX_BATCH_SIZE


def test_a_failed_commit_reserves_no_identities_for_later_batches(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Reserving before the write would make a retried batch refuse its own rows."""
    source, run, snapshot = _prepare(tmp_path)
    paths = shard_paths(run, SHARD)
    paths.parts.mkdir(parents=True, exist_ok=True)
    paths.receipts.mkdir(parents=True, exist_ok=True)
    context = worker_module._CommitContext(
        paths, snapshot, SHARD, _BATCH, snapshot.source_file(SHARD)
    )
    annotations = worker_module._batch_annotations(
        next(
            batch
            for _, batch in worker_module._iter_input_batches(
                worker_module.pq.ParquetFile(source / SHARD), _BATCH, 0
            )
        ),
        worker_module._text_analyser(_detector, fake_splitter()),
        worker_module.BoundedTextCache(8),
    )
    seen: set[str] = set()

    def refuse(*_args: object, **_kwargs: object) -> None:
        raise OSError("the volume went away")

    monkeypatch.setattr(worker_module, "write_annotation_part", refuse)

    with pytest.raises(OSError):
        worker_module._commit_batch(
            context,
            annotations,
            row_start=0,
            row_end=_BATCH,
            annotation_count=0,
            completed_parts=(),
            seen_identities=seen,
        )

    assert seen == set()


def test_a_part_that_fails_verification_reserves_none_of_its_identities(
    tmp_path: Path,
) -> None:
    """A part refused for its row count must not reserve the identities it carries."""
    source, run, snapshot = _prepare(tmp_path)
    process_shard(
        run,
        source,
        SHARD,
        detector=_detector,
        splitter=fake_splitter(),
        snapshot=snapshot,
        batch_size=_BATCH,
    )
    paths = shard_paths(run, SHARD)
    part_name = part_name_for_offset(0)
    receipt = worker_module.read_receipt(paths.receipt(part_name))
    seen: set[str] = set()

    with pytest.raises(CheckpointError):
        worker_module._verify_part_is_readable(
            paths.part(part_name),
            replace(receipt, row_count=receipt.row_count + 1),
            seen,
        )

    assert seen == set()


def test_a_committed_part_is_checked_against_the_identities_already_seen(
    tmp_path: Path,
) -> None:
    """Resume reads every committed part, so a duplicate across parts must be refused."""
    source, run, snapshot = _prepare(tmp_path)
    process_shard(
        run,
        source,
        SHARD,
        detector=_detector,
        splitter=fake_splitter(),
        snapshot=snapshot,
        batch_size=_BATCH,
    )
    paths = shard_paths(run, SHARD)
    part_name = part_name_for_offset(0)
    receipt = worker_module.read_receipt(paths.receipt(part_name))
    already_seen: set[str] = set()
    worker_module._verify_part_is_readable(paths.part(part_name), receipt, already_seen)

    with pytest.raises(CheckpointError) as caught:
        worker_module._verify_part_is_readable(paths.part(part_name), receipt, already_seen)

    assert "duplicate" in str(caught.value)


def test_a_missing_committed_receipt_names_the_part_it_belongs_to(tmp_path: Path) -> None:
    """The operator has to find one receipt among a shard's many parts."""
    source, run, snapshot = _prepare(tmp_path)
    process_shard(
        run,
        source,
        SHARD,
        detector=_detector,
        splitter=fake_splitter(),
        snapshot=snapshot,
        batch_size=_BATCH,
    )
    part_name = part_name_for_offset(0)
    shard_paths(run, SHARD).receipt(part_name).unlink()

    with pytest.raises(
        CheckpointError, match=exactly(f"committed receipt is missing: {part_name}")
    ):
        process_shard(
            run,
            source,
            SHARD,
            detector=_detector,
            splitter=fake_splitter(),
            snapshot=snapshot,
            batch_size=_BATCH,
        )


@pytest.mark.parametrize(
    ("changes", "message"),
    [
        (
            {"model_config_fingerprint": "c" * 64},
            "checkpoint belongs to a different detector configuration",
        ),
        (
            {"input_row_count": 99, "status": "paused"},
            "checkpoint input row count does not match the source file",
        ),
    ],
)
def test_a_checkpoint_that_does_not_bind_this_run_is_refused_in_full(
    tmp_path: Path, changes: dict[str, object], message: str
) -> None:
    """Each refusal names the binding that disagreed, in full."""
    source, run, snapshot = _prepare(tmp_path)
    process_shard(
        run,
        source,
        SHARD,
        detector=_detector,
        splitter=fake_splitter(),
        snapshot=snapshot,
        batch_size=_BATCH,
    )
    path = shard_paths(run, SHARD).checkpoint
    payload = json.loads(path.read_text(encoding="utf-8"))
    payload.update(changes)
    path.write_text(json.dumps(payload), encoding="utf-8")

    with pytest.raises(CheckpointError, match=exactly(message)):
        process_shard(
            run,
            source,
            SHARD,
            detector=_detector,
            splitter=fake_splitter(),
            snapshot=snapshot,
            batch_size=_BATCH,
        )
