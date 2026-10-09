"""Canonical-row selection is order-independent; canonical WKB is stable."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from itertools import permutations, product
from typing import cast

import duckdb
import pyarrow as pa
import pytest
from hypothesis import given
from hypothesis import settings as hypothesis_settings
from hypothesis import strategies as st
from shapely import to_wkb
from shapely.geometry import MultiPolygon, Point, Polygon

from osm_polygon_description_tag.dataset.canonical_rows import (
    _full_row_fingerprint_sql,
    _row_fingerprint,
    canonical_geometry_wkb,
    canonical_rows_sql,
    select_canonical_row,
)
from osm_polygon_description_tag.dataset.schema import KEY_VALUE_COLUMNS, SCHEMA
from osm_polygon_description_tag.dataset.storage import arrow_record
from tests.conftest import make_record_dict

_TIMESTAMPS = st.one_of(
    st.none(), st.sampled_from(["2024-01-01T00:00:00Z", "2025-06-01T12:00:00+00:00"])
)
_ROWS = st.fixed_dictionaries(
    {
        "osm_type": st.just("way"),
        "osm_id": st.just(7),
        "version": st.one_of(st.none(), st.integers(min_value=0, max_value=4)),
        "timestamp": _TIMESTAMPS,
        "source_pbf": st.sampled_from(["a.osm.pbf", "b.osm.pbf", "c.osm.pbf"]),
        "description": st.sampled_from(["one", "two", "three", " padded ", None]),
        "name": st.sampled_from(["x", "y", None]),
    }
)
_SQL_CANDIDATE = st.fixed_dictionaries(
    {
        "source_pbf": st.text(
            alphabet="abcdefghijklmnopqrstuvwxyz0123456789_-", min_size=1, max_size=8
        ).map(lambda name: f"{name}.osm.pbf"),
        "version": st.one_of(st.none(), st.integers(min_value=0, max_value=6)),
        "timestamp": st.one_of(
            st.none(),
            st.integers(min_value=0, max_value=10**12).map(
                lambda offset: datetime(2000, 1, 1, tzinfo=UTC) + timedelta(milliseconds=offset)
            ),
        ),
        "description": st.one_of(st.none(), st.text(max_size=12)),
        "area_m2": st.floats(min_value=0.1, max_value=1e7, allow_nan=False),
    }
)
_SQL_CONNECTION = duckdb.connect()
_SQL_COLUMNS = SCHEMA.names
_SQL_POLYGON = Polygon([(0, 0), (0, 1), (1, 1), (1, 0)])


def _key(row: object) -> str:
    return repr(sorted(row.items()))  # type: ignore[attr-defined]


@given(st.lists(_ROWS, min_size=1, max_size=6), st.randoms(use_true_random=False))
def test_selection_ignores_input_order_and_returns_a_member(
    rows: list[dict[str, object]], rng: object
) -> None:
    shuffled = list(rows)
    rng.shuffle(shuffled)  # type: ignore[attr-defined]

    winner = select_canonical_row(rows)

    assert _key(select_canonical_row(shuffled)) == _key(winner)
    assert winner in rows


def test_invalid_timestamp_is_rejected_for_every_input_permutation() -> None:
    rows = [
        {"version": 1, "timestamp": None, "source_pbf": "same.osm.pbf"},
        {"version": 1, "timestamp": "bad", "source_pbf": "same.osm.pbf"},
    ]

    for ordered_rows in permutations(rows):
        with pytest.raises(
            ValueError,
            match="timestamp must be null or a valid ISO-8601 value",
        ):
            select_canonical_row(ordered_rows)


@given(st.lists(_ROWS, min_size=1, max_size=6), st.randoms(use_true_random=False))
def test_selection_with_required_text_ignores_input_order(
    rows: list[dict[str, object]], rng: object
) -> None:
    usable = [row for row in rows if row["description"] in {"one", "two", "three"}]
    if not usable:
        return
    shuffled = list(rows)
    rng.shuffle(shuffled)  # type: ignore[attr-defined]

    winner = select_canonical_row(rows, require_successful_text=True)

    assert _key(select_canonical_row(shuffled, require_successful_text=True)) == _key(winner)
    assert winner in usable


@given(st.lists(_ROWS, min_size=1, max_size=6))
def test_the_winner_has_the_highest_version_present(rows: list[dict[str, object]]) -> None:
    winner = select_canonical_row(rows)

    versions = [row["version"] for row in rows if isinstance(row["version"], int)]
    if versions:
        assert winner["version"] == max(versions)


# DuckDB plans the fingerprint query for every example, so one example takes about
# 0.1 s per query and can exceed the dev profile's 200 ms deadline on a cold start.
# The ci and mutation profiles already disable the deadline; this keeps dev aligned.
@hypothesis_settings(deadline=None)
@given(st.lists(_SQL_CANDIDATE, min_size=1, max_size=5))
def test_duckdb_canonical_rows_match_python_selection(
    candidates: list[dict[str, object]],
) -> None:
    rows = [
        {
            **make_record_dict(
                _SQL_POLYGON,
                {"description": "seed"},
                osm_id=77,
                source_pbf=str(candidate["source_pbf"]),
            ),
            **candidate,
        }
        for candidate in candidates
    ]
    # Guarantee at least one eligible candidate for the text-aware selector.
    rows[0]["description"] = "eligible"
    table = pa.Table.from_pylist([arrow_record(row) for row in rows], schema=SCHEMA)
    # Compare the Python selector against the exact millisecond-resolution rows
    # DuckDB receives, including their list-of-struct storage representation.
    rows = table.to_pylist()
    _SQL_CONNECTION.register("canonical_candidates", table)
    try:
        for require_successful_text in (False, True):
            expected = select_canonical_row(rows, require_successful_text=require_successful_text)
            query = canonical_rows_sql(
                "canonical_candidates",
                _SQL_COLUMNS,
                require_successful_text=require_successful_text,
            )
            actual = _SQL_CONNECTION.execute(query).to_arrow_table().to_pylist()

            assert actual == [arrow_record(expected)]
    finally:
        _SQL_CONNECTION.unregister("canonical_candidates")


def test_reported_null_timestamp_candidates_have_the_same_winner_in_both_selectors() -> None:
    base = make_record_dict(
        _SQL_POLYGON,
        {"description": "seed"},
        osm_id=77,
        source_pbf="0.osm.pbf",
    )
    rows = [
        {
            **base,
            "version": version,
            "timestamp": None,
            "description": description,
            "area_m2": area_m2,
        }
        for version, description, area_m2 in (
            (None, None, 1.0),
            (0, None, 0.125),
            (0, "00\\^_", 3.0),
        )
    ]

    for ordered_rows in permutations(rows):
        table = pa.Table.from_pylist([arrow_record(row) for row in ordered_rows], schema=SCHEMA)
        table_rows = table.to_pylist()
        _SQL_CONNECTION.register("canonical_candidates", table)
        try:
            for require_successful_text in (False, True):
                expected = select_canonical_row(
                    table_rows,
                    require_successful_text=require_successful_text,
                )
                expected_signature = (
                    expected["version"],
                    expected["timestamp"],
                    expected["description"],
                    expected["area_m2"],
                )
                assert expected_signature == (0, None, "00\\^_", 3.0)

                query = canonical_rows_sql(
                    "canonical_candidates",
                    _SQL_COLUMNS,
                    require_successful_text=require_successful_text,
                )
                actual = _SQL_CONNECTION.execute(query).to_arrow_table().to_pylist()
                assert actual == [arrow_record(expected)]
        finally:
            _SQL_CONNECTION.unregister("canonical_candidates")


def test_missing_version_sorts_after_any_present_version_in_both_selectors() -> None:
    missing_version = make_record_dict(
        _SQL_POLYGON,
        {"description": "missing version"},
        osm_id=77,
        source_pbf="a.osm.pbf",
    )
    missing_version.update(version=None, timestamp=None)
    lowest_version = make_record_dict(
        _SQL_POLYGON,
        {"description": "present version"},
        osm_id=77,
        source_pbf="z.osm.pbf",
    )
    lowest_version.update(version=-1, timestamp=None)

    for ordered_rows in permutations((missing_version, lowest_version)):
        table = pa.Table.from_pylist([arrow_record(row) for row in ordered_rows], schema=SCHEMA)
        table_rows = table.to_pylist()
        _SQL_CONNECTION.register("canonical_candidates", table)
        try:
            assert select_canonical_row(table_rows) == arrow_record(lowest_version)

            query = canonical_rows_sql("canonical_candidates", _SQL_COLUMNS)
            actual = _SQL_CONNECTION.execute(query).to_arrow_table().to_pylist()
            assert actual == [arrow_record(lowest_version)]
        finally:
            _SQL_CONNECTION.unregister("canonical_candidates")


def test_a_valid_pre_epoch_timestamp_ranks_before_a_missing_timestamp_in_both_selectors() -> None:
    missing = {
        **make_record_dict(
            _SQL_POLYGON,
            {"description": "missing time"},
            osm_id=77,
            source_pbf="a.osm.pbf",
        ),
        "version": 1,
        "timestamp": None,
        "area_m2": 1.0,
    }
    historic_timestamp = datetime(1960, 1, 1, tzinfo=UTC)
    historic = {
        **make_record_dict(
            _SQL_POLYGON,
            {"description": "historic time"},
            osm_id=77,
            source_pbf="z.osm.pbf",
        ),
        "version": 1,
        "timestamp": historic_timestamp,
        "area_m2": 2.0,
    }
    expected_signature = ("z.osm.pbf", historic_timestamp, 2.0)

    for ordered_rows in permutations((missing, historic)):
        table = pa.Table.from_pylist([arrow_record(row) for row in ordered_rows], schema=SCHEMA)
        table_rows = table.to_pylist()
        _SQL_CONNECTION.register("canonical_candidates", table)
        try:
            for require_successful_text in (False, True):
                expected = select_canonical_row(
                    table_rows,
                    require_successful_text=require_successful_text,
                )
                query = canonical_rows_sql(
                    "canonical_candidates",
                    _SQL_COLUMNS,
                    require_successful_text=require_successful_text,
                )
                actual = _SQL_CONNECTION.execute(query).to_arrow_table().to_pylist()
                actual_signature = (
                    actual[0]["source_pbf"],
                    actual[0]["timestamp"],
                    actual[0]["area_m2"],
                )
                python_signature = (
                    expected["source_pbf"],
                    expected["timestamp"],
                    expected["area_m2"],
                )
                assert actual_signature == expected_signature
                assert python_signature == expected_signature
                assert actual == [arrow_record(expected)]
        finally:
            _SQL_CONNECTION.unregister("canonical_candidates")


_COORDS = st.floats(min_value=-170, max_value=170, allow_nan=False, allow_infinity=False)


@given(_COORDS, _COORDS, st.floats(min_value=0.01, max_value=5), st.booleans())
def test_canonical_wkb_is_idempotent_and_little_endian_2d(
    x: float, y: float, size: float, use_point: bool
) -> None:
    geometry = (
        Point(x, y) if use_point else Polygon([(x, y), (x + size, y), (x + size, y + size), (x, y)])
    )
    once = canonical_geometry_wkb(to_wkb(geometry, byte_order=0))

    assert canonical_geometry_wkb(once) == once
    assert once[0] == 1
    assert once == canonical_geometry_wkb(to_wkb(geometry, byte_order=1, output_dimension=3))


def test_topologically_equal_polygons_keep_their_distinct_ring_order() -> None:
    """Topological equality is not a byte-canonicalization contract for ring order."""
    first = Polygon([(0, 0), (2, 0), (2, 1), (0, 1), (0, 0)])
    rotated = Polygon([(2, 1), (0, 1), (0, 0), (2, 0), (2, 1)])

    assert first.equals(rotated)
    assert canonical_geometry_wkb(to_wkb(first)) != canonical_geometry_wkb(to_wkb(rotated))


_MAP_SCHEMA = pa.schema(
    [
        pa.field(field.name, pa.map_(pa.string(), pa.string()), nullable=field.nullable)
        if field.name in KEY_VALUE_COLUMNS
        else field
        for field in SCHEMA
    ]
)


def _candidate_table(rows: list[dict[str, object]], *, maps: bool) -> pa.Table:
    """Return the rows as Arrow, optionally with key/value columns as maps (the stats view)."""
    records = [arrow_record(row) for row in rows]
    if not maps:
        return pa.Table.from_pylist(records, schema=SCHEMA)
    as_maps: list[dict[str, object]] = []
    for record in records:
        converted = dict(record)
        for name in KEY_VALUE_COLUMNS:
            entries = cast("list[dict[str, str]]", record[name])
            converted[name] = [(entry["key"], entry["value"]) for entry in entries]
        as_maps.append(converted)
    return pa.Table.from_pylist(as_maps, schema=_MAP_SCHEMA)


@pytest.mark.parametrize("maps", [False, True], ids=["list", "map"])
def test_reported_control_character_tie_has_the_same_winner_in_both_selectors(maps: bool) -> None:
    """Rows tied on version, timestamp and source are ordered by the row fingerprint.

    The fingerprint must be the same value in Python and DuckDB, otherwise the
    two selectors disagree on which row wins. Here the descriptions are a null
    and U+001E, which the text rules treat as not usable.
    """
    base = make_record_dict(
        _SQL_POLYGON, {"description": "seed"}, osm_id=77, source_pbf="0.osm.pbf"
    )
    rows = [
        {**base, "version": None, "timestamp": None, "description": None, "area_m2": 0.5},
        {**base, "version": None, "timestamp": None, "description": "\x1e", "area_m2": 1.0},
    ]

    for ordered_rows in permutations(rows):
        table = _candidate_table(list(ordered_rows), maps=maps)
        table_rows = table.to_pylist()
        _SQL_CONNECTION.register("canonical_candidates", table)
        try:
            expected = select_canonical_row(table_rows)
            query = canonical_rows_sql(
                "canonical_candidates", _SQL_COLUMNS, key_value_columns_are_maps=maps
            )
            actual = _SQL_CONNECTION.execute(query).to_arrow_table().to_pylist()

            assert actual == [expected]
        finally:
            _SQL_CONNECTION.unregister("canonical_candidates")


_FINGERPRINT_TEXTS = (
    *(chr(code) for code in range(0x20)),
    '"',
    "\\",
    "\\u001E",
    "\x7f",
    "\u2028",
    "é",
    "\U0001f600",
    "",
)
_FINGERPRINT_AREAS = (1e-05, 0.1, 1e16, 12345.678)
_FINGERPRINT_GEOMETRIES = (
    _SQL_POLYGON,
    MultiPolygon(
        [
            Polygon([(0, 0), (0, 2), (2, 2), (2, 0)], [[(0.5, 0.5), (1.5, 0.5), (1.5, 1.5)]]),
            Polygon([(5, 5), (5, 6), (6, 6), (6, 5)]),
        ]
    ),
)


@pytest.mark.parametrize("maps", [False, True], ids=["list", "map"])
def test_sql_row_fingerprint_matches_python_row_fingerprint_for_edge_cases(maps: bool) -> None:
    """The DuckDB tie-break fingerprint must be byte-identical to the Python one.

    The Python fingerprint is the SHA-256 of ``json.dumps`` output. Control
    characters are escaped with lowercase hex and floats use Python's repr, so
    the DuckDB payload must reproduce both exactly, for list and map columns.
    """
    rows = []
    for index, (text, area_m2, geometry) in enumerate(
        product(_FINGERPRINT_TEXTS, _FINGERPRINT_AREAS, _FINGERPRINT_GEOMETRIES)
    ):
        base = make_record_dict(
            geometry, {"description": "seed"}, osm_id=77, source_pbf=f"{index}.osm.pbf"
        )
        rows.append(
            {
                **base,
                "name": text,
                "description": text,
                "localized_names": [{"key": "fr", "value": text}],
                "localized_descriptions": [{"key": "fr", "value": text}],
                "tags": [{"key": "description", "value": text}, {"key": "name", "value": text}],
                "area_m2": area_m2,
            }
        )
    table = _candidate_table(rows, maps=maps)
    table_rows = table.to_pylist()
    _SQL_CONNECTION.register("canonical_candidates", table)
    try:
        query = (
            "SELECT source_pbf, "  # noqa: S608 - fixed internal SQL
            f"{_full_row_fingerprint_sql(key_value_columns_are_maps=maps)} AS fingerprint "
            "FROM canonical_candidates"
        )
        actual = dict(_SQL_CONNECTION.execute(query).fetchall())
    finally:
        _SQL_CONNECTION.unregister("canonical_candidates")

    assert actual == {row["source_pbf"]: _row_fingerprint(row) for row in table_rows}
