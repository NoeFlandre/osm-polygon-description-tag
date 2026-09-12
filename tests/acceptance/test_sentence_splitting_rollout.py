"""End-to-end: a shard is processed once, and every row says what happened to it.

This is the behaviour the Grid'5000 run publishes. One pass over one shard
detects the language and, only for a language the splitter was trained on,
splits the description into sentences. Every other row is published unsplit with
the reason recorded, and the run validates as complete either way.
"""

from __future__ import annotations

from pathlib import Path

import pyarrow.parquet as pq
import pytest
from shapely.geometry import Polygon

from osm_polygon_description_tag.dataset.languages.checkpoint import shard_paths
from osm_polygon_description_tag.dataset.languages.models import (
    LanguageResult,
    LanguageStatus,
)
from osm_polygon_description_tag.dataset.languages.snapshot import prepare_snapshot
from osm_polygon_description_tag.dataset.languages.validation import validate_run
from osm_polygon_description_tag.dataset.languages.worker import process_shard
from osm_polygon_description_tag.dataset.sentences.models import SentenceSplitStatus
from osm_polygon_description_tag.storage import write_geoparquet
from tests.conftest import make_record_dict
from tests.helpers.sentences import fake_splitter

SHARD = "region.parquet"

# One description per language outcome the pipeline can produce.
_DESCRIPTIONS: tuple[tuple[str, str | None, LanguageStatus], ...] = (
    ("A quiet park. It has two benches.", "eng", LanguageStatus.DETECTED),
    ("Un parc tranquille. Il a deux bancs.", "fra", LanguageStatus.DETECTED),
    ("Tihi park. Ima dvije klupe.", "hrv", LanguageStatus.DETECTED),
    ("Ngicela usizo. Ngiyabonga.", "zul", LanguageStatus.DETECTED),
    ("????", None, LanguageStatus.NON_LINGUISTIC),
    ("xyzzy plugh", None, LanguageStatus.UNCERTAIN),
)


def _detector(text: str) -> LanguageResult:
    for original, code, status in _DESCRIPTIONS:
        if text == original:
            if status is LanguageStatus.DETECTED:
                return LanguageResult(code, 0.99, 0.01, 0.98, status, "detected")
            return LanguageResult(None, None, None, None, status, str(status))
    raise AssertionError(f"unexpected description: {text!r}")


@pytest.fixture
def processed_shard(tmp_path: Path) -> Path:
    source = tmp_path / "source"
    source.mkdir(parents=True)
    write_geoparquet(
        (
            make_record_dict(
                Polygon([(0, 0), (0, 1), (1, 1), (1, 0)]),
                {"description": text},
                osm_id=index + 1,
            )
            for index, (text, _code, _status) in enumerate(_DESCRIPTIONS)
        ),
        source / SHARD,
        batch_size=3,
    )
    run = tmp_path / "run"
    snapshot = prepare_snapshot(source, run, code_fingerprint="a" * 64, lock_fingerprint="b" * 64)

    process_shard(
        run,
        source,
        SHARD,
        detector=_detector,
        splitter=fake_splitter(),
        snapshot=snapshot,
        batch_size=3,
    )
    return run


def _rows(run: Path) -> dict[str, dict[str, object]]:
    paths = shard_paths(run, SHARD)
    rows: dict[str, dict[str, object]] = {}
    for part in sorted(paths.parts.iterdir()):
        for row in pq.read_table(part).to_pylist():
            rows[str(row["original_text"])] = row
    return rows


def test_a_supported_language_is_split_into_sentences(processed_shard: Path) -> None:
    row = _rows(processed_shard)["A quiet park. It has two benches."]

    assert row["language_code"] == "eng"
    assert row["split_status"] == str(SentenceSplitStatus.SPLIT)
    assert row["sentences"] == ["A quiet park.", "It has two benches."]
    assert row["sentence_count"] == 2


def test_every_supported_language_in_the_shard_is_split(processed_shard: Path) -> None:
    rows = _rows(processed_shard)

    french = rows["Un parc tranquille. Il a deux bancs."]
    assert french["split_status"] == str(SentenceSplitStatus.SPLIT)
    assert french["sentences"] == ["Un parc tranquille.", "Il a deux bancs."]


def test_a_detected_but_unsupported_language_is_skipped_and_marked(
    processed_shard: Path,
) -> None:
    """Croatian is detected confidently and is not one of SaT's 85 languages."""
    row = _rows(processed_shard)["Tihi park. Ima dvije klupe."]

    assert row["language_code"] == "hrv"
    assert row["status"] == str(LanguageStatus.DETECTED)
    assert row["split_status"] == str(SentenceSplitStatus.UNSUPPORTED_LANGUAGE)
    assert row["split_reason"] == "unsupported_language_hrv"
    assert row["sentences"] == []
    assert row["sentence_count"] == 0


def test_a_supported_language_beyond_the_detectors_own_range_still_splits(
    processed_shard: Path,
) -> None:
    """Zulu is in SaT's 85, so a detected Zulu description is split."""
    row = _rows(processed_shard)["Ngicela usizo. Ngiyabonga."]

    assert row["language_code"] == "zul"
    assert row["split_status"] == str(SentenceSplitStatus.SPLIT)


@pytest.mark.parametrize(
    ("text", "reason"),
    [
        ("????", "not_detected_non_linguistic"),
        ("xyzzy plugh", "not_detected_uncertain"),
    ],
)
def test_a_description_without_a_settled_language_is_skipped_and_marked(
    processed_shard: Path, text: str, reason: str
) -> None:
    row = _rows(processed_shard)[text]

    assert row["language_code"] is None
    assert row["split_status"] == str(SentenceSplitStatus.NOT_DETECTED)
    assert row["split_reason"] == reason
    assert row["sentences"] == []


def test_the_run_is_complete_and_every_row_carries_a_split_outcome(
    processed_shard: Path,
) -> None:
    """Skipping a description must never make the run incomplete."""
    report = validate_run(processed_shard)
    rows = _rows(processed_shard)

    assert report.is_complete
    assert report.issues == ()
    assert len(rows) == len(_DESCRIPTIONS)
    assert all(row["split_reason"] for row in rows.values())
    assert sum(1 for row in rows.values() if row["sentences"]) == 3
