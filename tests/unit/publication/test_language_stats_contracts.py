"""Exact statistics contracts of the language export accumulator.

`language-v1/stats.json` is the only quantitative claim published about the
annotations, so its counts and its ranking are asserted by value: a ranking
that ties arbitrarily, an object count that pairs the wrong columns, or a
truncation at the wrong length would all publish a wrong number silently.
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import cast

import pyarrow as pa
import pytest

from osm_polygon_description_tag.publication.language import _StatsAccumulator


def _batch(rows: list[dict[str, object]]) -> pa.RecordBatch:
    schema = pa.schema(
        [
            pa.field("status", pa.string()),
            pa.field("language_code", pa.string()),
            pa.field("tag_key", pa.string()),
            pa.field("osm_type", pa.string()),
            pa.field("osm_id", pa.int64()),
        ]
    )
    columns = {name: [row.get(name) for row in rows] for name in schema.names}
    return pa.RecordBatch.from_pydict(columns, schema=schema)


def _row(**overrides: object) -> dict[str, object]:
    return {
        "status": "detected",
        "language_code": "eng",
        "tag_key": "description",
        "osm_type": "way",
        "osm_id": 1,
        **overrides,
    }


def test_statuses_tag_keys_and_objects_are_counted_separately() -> None:
    accumulator = _StatsAccumulator()
    accumulator.observe(
        _batch(
            [
                _row(osm_id=1),
                _row(osm_id=1, tag_key="description:fr", language_code="fra"),
                _row(osm_id=2, status="uncertain", language_code=None),
                _row(osm_id=3, status="non_linguistic", language_code=None),
            ]
        )
    )

    stats = accumulator.result()

    assert stats.annotation_count == 4
    assert stats.object_count == 3
    assert stats.base_description_count == 3
    assert stats.localized_description_count == 1
    assert stats.detected_count == 2
    assert stats.uncertain_count == 1
    assert stats.non_linguistic_count == 1
    assert stats.distinct_language_count == 2


def test_the_same_osm_id_under_a_different_type_is_a_different_object() -> None:
    """The object key is the pair, so pairing the wrong columns would merge them."""
    accumulator = _StatsAccumulator()
    accumulator.observe(
        _batch([_row(osm_type="way", osm_id=7), _row(osm_type="relation", osm_id=7)])
    )

    assert accumulator.result().object_count == 2


def test_top_languages_are_ranked_by_descending_count_then_by_code() -> None:
    accumulator = _StatsAccumulator()
    accumulator.observe(
        _batch(
            [
                *[_row(language_code="fra", osm_id=index) for index in range(3)],
                *[_row(language_code="deu", osm_id=100 + index) for index in range(2)],
                *[_row(language_code="ces", osm_id=200 + index) for index in range(2)],
                _row(language_code="eng", osm_id=300),
            ]
        )
    )

    assert accumulator.result().top_languages == (("fra", 3), ("ces", 2), ("deu", 2), ("eng", 1))


def test_the_ranking_keeps_exactly_twenty_languages() -> None:
    accumulator = _StatsAccumulator()
    codes = [f"l{index:02d}" for index in range(25)]
    accumulator.observe(
        _batch(
            [_row(language_code=code, osm_id=index) for index, code in enumerate(reversed(codes))]
        )
    )

    top = accumulator.result().top_languages

    assert len(top) == 20
    assert [code for code, _ in top] == codes[:20]


def test_rows_without_a_language_never_enter_the_ranking() -> None:
    accumulator = _StatsAccumulator()
    accumulator.observe(
        _batch([_row(status="uncertain", language_code=None), _row(language_code="fra")])
    )

    stats = accumulator.result()

    assert stats.top_languages == (("fra", 1),)
    assert stats.distinct_language_count == 1


def test_object_columns_must_have_the_same_length() -> None:
    """A truncated object column must fail instead of silently dropping rows."""
    batch = cast(
        pa.RecordBatch,
        SimpleNamespace(
            num_rows=2,
            to_pydict=lambda: {
                "status": ["detected", "detected"],
                "language_code": ["eng", "eng"],
                "tag_key": ["description", "description"],
                "osm_type": ["way"],
                "osm_id": [1, 2],
            },
        ),
    )

    with pytest.raises(ValueError):
        _StatsAccumulator().observe(batch)


def test_observing_multiple_batches_accumulates_the_total() -> None:
    accumulator = _StatsAccumulator()

    accumulator.observe(_batch([_row(osm_id=1)]))
    accumulator.observe(_batch([_row(osm_id=2), _row(osm_id=3)]))

    assert accumulator.result().annotation_count == 3
