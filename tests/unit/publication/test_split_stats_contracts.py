"""The published card must report what happened to splitting, not just detection.

A consumer reading the dataset card needs to know how much of it carries
sentences and why the rest does not. These counts come from the exported files
themselves, so they cannot drift from what was published.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from shapely.geometry import Polygon

from osm_polygon_description_tag.dataset.languages.models import LanguageResult, LanguageStatus
from osm_polygon_description_tag.dataset.languages.snapshot import prepare_snapshot
from osm_polygon_description_tag.dataset.languages.worker import process_shard
from osm_polygon_description_tag.publication.language import (
    export_language_annotations,
    render_language_card_section,
)
from osm_polygon_description_tag.storage import write_geoparquet
from tests.conftest import make_record_dict
from tests.helpers.sentences import fake_splitter

SHARD = "region.parquet"

_TEXTS: tuple[tuple[str, str | None, LanguageStatus], ...] = (
    ("A park. It has benches.", "eng", LanguageStatus.DETECTED),
    ("Un parc. Il a des bancs.", "fra", LanguageStatus.DETECTED),
    ("Tihi park. Ima klupe.", "hrv", LanguageStatus.DETECTED),
    ("xyzzy plugh", None, LanguageStatus.UNCERTAIN),
)


def _detector(text: str) -> LanguageResult:
    for original, code, status in _TEXTS:
        if text == original:
            if status is LanguageStatus.DETECTED:
                return LanguageResult(code, 0.99, 0.01, 0.98, status, "detected")
            return LanguageResult(None, None, None, None, status, str(status))
    raise AssertionError(text)


@pytest.fixture
def export(tmp_path: Path) -> object:
    source = tmp_path / "source"
    source.mkdir(parents=True)
    write_geoparquet(
        (
            make_record_dict(
                Polygon([(0, 0), (0, 1), (1, 1), (1, 0)]),
                {"description": text},
                osm_id=index + 1,
            )
            for index, (text, _c, _s) in enumerate(_TEXTS)
        ),
        source / SHARD,
        batch_size=2,
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
        batch_size=2,
    )
    return export_language_annotations(run, tmp_path / "export")


def test_the_export_counts_what_was_split_and_what_was_skipped(export: object) -> None:
    stats = export.stats  # type: ignore[attr-defined]

    assert stats.split_count == 2
    assert stats.unsupported_language_count == 1
    assert stats.not_detected_count == 1
    assert stats.sentence_count == 4
    assert (
        stats.split_count + stats.unsupported_language_count + stats.not_detected_count
        == stats.annotation_count
    )


def test_the_stats_payload_carries_the_split_counts(export: object) -> None:
    payload = export.stats.to_payload()  # type: ignore[attr-defined]

    assert payload["split_count"] == 2
    assert payload["unsupported_language_count"] == 1
    assert payload["not_detected_count"] == 1
    assert payload["sentence_count"] == 4


def test_the_card_section_reports_sentence_splitting(export: object) -> None:
    section = render_language_card_section(export)  # type: ignore[arg-type]

    assert "| Split into sentences | 2 |" in section
    assert "| Sentences | 4 |" in section
    # The two skip counts are derivable from the totals and were dropped to
    # keep the card section short; the split totals still have to be exact.
    assert "Skipped, language unsupported" not in section
