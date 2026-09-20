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


def _unsupported_columns(codes: list[str | None], statuses: list[str]) -> dict[str, list[object]]:
    return {"language_code": list(codes), "split_status": list(statuses)}


def test_only_rows_left_unsplit_for_their_language_are_counted() -> None:
    """The filter has to match on both the status *and* a present language.

    A split row, a row with no detected language, and an unsupported row with a
    null code must all be ignored; only the last kind names a language the
    splitter could not handle.
    """
    from osm_polygon_description_tag.publication.language import _unsupported_languages_in

    columns = _unsupported_columns(
        ["tso", "eng", None, "vec", None],
        [
            "unsupported_language",
            "split",
            "not_detected",
            "unsupported_language",
            "unsupported_language",
        ],
    )

    assert sorted(_unsupported_languages_in(columns)) == ["tso", "vec"]


def test_no_unsupported_rows_yields_nothing() -> None:
    from osm_polygon_description_tag.publication.language import _unsupported_languages_in

    columns = _unsupported_columns(["eng", "fra"], ["split", "split"])

    assert list(_unsupported_languages_in(columns)) == []


def test_mismatched_column_lengths_are_rejected() -> None:
    """Two columns of different lengths mean the batch was mis-assembled.

    ``zip`` would silently stop at the shorter one and under-count the
    unsupported languages, so the pairing is strict.
    """
    from osm_polygon_description_tag.publication.language import _unsupported_languages_in

    columns = _unsupported_columns(
        ["tso", "vec"], ["unsupported_language", "unsupported_language", "unsupported_language"]
    )

    with pytest.raises(ValueError, match="zip"):
        list(_unsupported_languages_in(columns))


def test_unsupported_languages_are_reported_largest_first_and_capped() -> None:
    """The published table takes the twenty largest, ties broken by code.

    Asserted through ``result()`` rather than by re-sorting the counter here:
    the ordering and the cap belong to the published field, and a test that
    re-implements them cannot notice the field losing either one.
    """
    from osm_polygon_description_tag.publication.language import _StatsAccumulator

    accumulator = _StatsAccumulator()
    rows: list[dict[str, object]] = []
    osm_id = 0
    # Twenty-five distinct languages with descending counts, so the cap is
    # observable, and two sharing a count, so the tie-break is observable.
    for index in range(25):
        occurrences = 25 - index
        if index == 1:
            occurrences = 25
        for _ in range(occurrences):
            osm_id += 1
            rows.append(_row(f"l{index:02d}", "unsupported_language", osm_id=osm_id))
    accumulator.observe(_annotation_batch(rows))

    stats = accumulator.result()

    assert stats.unsupported_distinct_count == 25
    assert len(stats.top_unsupported_languages) == 20
    # "l00" and "l01" both occur 25 times, so the code decides which comes first.
    assert stats.top_unsupported_languages[0] == ("l00", 25)
    assert stats.top_unsupported_languages[1] == ("l01", 25)
    # Descending, and truncated before the five smallest.
    assert stats.top_unsupported_languages[-1] == ("l19", 6)
    assert [count for _code, count in stats.top_unsupported_languages] == sorted(
        (count for _code, count in stats.top_unsupported_languages), reverse=True
    )


def _annotation_batch(rows: list[dict[str, object]]):
    import pyarrow as pa

    keys = (
        "status",
        "language_code",
        "tag_key",
        "osm_type",
        "osm_id",
        "split_status",
        "sentence_count",
    )
    return pa.record_batch([pa.array([row[key] for row in rows]) for key in keys], names=list(keys))


def _row(code: str | None, split: str, *, osm_id: int = 1) -> dict[str, object]:
    return {
        "status": "detected" if code else "uncertain",
        "language_code": code,
        "tag_key": "description",
        "osm_type": "way",
        "osm_id": osm_id,
        "split_status": split,
        "sentence_count": 1,
    }


def test_the_result_reports_every_distinct_unsupported_language() -> None:
    """The distinct count is the whole set, not the truncated table."""
    from osm_polygon_description_tag.publication.language import _StatsAccumulator

    accumulator = _StatsAccumulator()
    rows = [
        _row("tso", "unsupported_language", osm_id=1),
        _row("tso", "unsupported_language", osm_id=2),
        _row("vec", "unsupported_language", osm_id=3),
        _row("eng", "split", osm_id=4),
        _row(None, "not_detected", osm_id=5),
    ]
    accumulator.observe(_annotation_batch(rows))

    result = accumulator.result()

    assert result.unsupported_distinct_count == 2
    assert result.top_unsupported_languages == (("tso", 2), ("vec", 1))
    assert result.unsupported_language_count == 3
    assert result.split_count == 1
    assert result.not_detected_count == 1


def test_equal_unsupported_counts_are_ordered_by_language_code() -> None:
    """Ties must resolve deterministically or the published table is unstable."""
    from osm_polygon_description_tag.publication.language import _StatsAccumulator

    accumulator = _StatsAccumulator()
    accumulator.observe(
        _annotation_batch(
            [
                _row("zul", "unsupported_language", osm_id=1),
                _row("ast", "unsupported_language", osm_id=2),
            ]
        )
    )

    assert accumulator.result().top_unsupported_languages == (("ast", 1), ("zul", 1))


def test_the_eligible_total_is_the_two_splitting_outcomes_added(export: object) -> None:
    """Eligible is split plus unsupported, and the coverage divides by it.

    Both counts have to be non-zero for the arithmetic to be visible at all:
    with nothing unsupported, adding and subtracting agree.
    """
    from dataclasses import replace

    stats = replace(export.stats, split_count=7, unsupported_language_count=3)
    section = render_language_card_section(replace(export, stats=stats))

    assert "| Eligible text units | 10 |" in section
    assert "| Split (language supported) | 7 |" in section
    assert "| Unsplit (language unsupported) | 3 |" in section
    assert "| Coverage | 70.0000% |" in section
    assert "| Unsupported | 30.0000% |" in section
