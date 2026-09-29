"""Acceptance: a language worker that stops mid-shard resumes where it left off.

User story: as an operator on a batch scheduler, a killed job must not redo the
work already committed. The restarted worker finishes the remaining parts, and
the finished run validates.
"""

from __future__ import annotations

from pathlib import Path

import pyarrow.parquet as pq
import pytest
from shapely.geometry import Polygon

from osm_polygon_description_tag.dataset.languages.checkpoint import shard_paths
from osm_polygon_description_tag.dataset.languages.models import LanguageResult, LanguageStatus
from osm_polygon_description_tag.dataset.languages.snapshot import prepare_snapshot
from osm_polygon_description_tag.dataset.languages.validation import validate_run
from osm_polygon_description_tag.dataset.languages.worker import process_shard
from osm_polygon_description_tag.dataset.storage import write_geoparquet
from tests.conftest import make_record_dict
from tests.helpers.sentences import fake_splitter

SHARD = "region.parquet"
ROWS = 6
BATCH = 2


class _Detector:
    """Detects every text as English; can be killed on its n-th call."""

    def __init__(self, die_on_call: int | None = None) -> None:
        self.calls: list[str] = []
        self.die_on_call = die_on_call

    def __call__(self, text: str) -> LanguageResult:
        self.calls.append(text)
        if self.die_on_call is not None and len(self.calls) == self.die_on_call:
            raise KeyboardInterrupt
        return LanguageResult("eng", 0.99, 0.01, 0.98, LanguageStatus.DETECTED, "detected")


@pytest.fixture
def staged(tmp_path: Path) -> tuple[Path, Path]:
    source = tmp_path / "source"
    source.mkdir()
    write_geoparquet(
        (
            make_record_dict(
                Polygon([(0, 0), (0, 1), (1, 1), (1, 0)]),
                {"description": f"Park number {index}. It has benches."},
                osm_id=index,
            )
            for index in range(1, ROWS + 1)
        ),
        source / SHARD,
        batch_size=BATCH,
    )
    return source, tmp_path / "run"


def _process(source: Path, run: Path, detector: _Detector) -> None:
    snapshot = prepare_snapshot(source, run, code_fingerprint="a" * 64, lock_fingerprint="b" * 64)
    process_shard(
        run,
        source,
        SHARD,
        detector=detector,
        splitter=fake_splitter(),
        snapshot=snapshot,
        batch_size=BATCH,
    )


def test_given_a_killed_worker_when_restarted_then_only_the_rest_is_detected(
    staged: tuple[Path, Path],
) -> None:
    source, run = staged
    first = _Detector(die_on_call=BATCH + 1)  # dies on the first row of the second batch
    with pytest.raises(KeyboardInterrupt):
        _process(source, run, first)
    committed = sorted(shard_paths(run, SHARD).parts.iterdir())

    second = _Detector()
    _process(source, run, second)

    assert len(committed) == 1
    assert len(second.calls) == ROWS - BATCH  # the committed batch is not detected again
    assert set(first.calls[:BATCH]).isdisjoint(second.calls)


def test_given_a_resumed_worker_when_it_finishes_then_the_run_is_complete_and_valid(
    staged: tuple[Path, Path],
) -> None:
    source, run = staged
    with pytest.raises(KeyboardInterrupt):
        _process(source, run, _Detector(die_on_call=BATCH + 1))

    _process(source, run, _Detector())

    parts = sorted(shard_paths(run, SHARD).parts.iterdir())
    rows = [row for part in parts for row in pq.read_table(part).to_pylist()]
    assert len(parts) == ROWS // BATCH
    assert sorted(int(row["osm_id"]) for row in rows) == list(range(1, ROWS + 1))
    assert validate_run(run) is not None


def test_given_a_finished_shard_when_restarted_then_no_row_is_detected_again(
    staged: tuple[Path, Path],
) -> None:
    source, run = staged
    _process(source, run, _Detector())

    again = _Detector()
    _process(source, run, again)

    assert again.calls == []
