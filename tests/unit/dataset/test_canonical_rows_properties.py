"""Canonical-row selection is order-independent; canonical WKB is stable."""

from __future__ import annotations

from hypothesis import given
from hypothesis import strategies as st
from shapely import to_wkb
from shapely.geometry import Point, Polygon

from osm_polygon_description_tag.dataset.canonical_rows import (
    canonical_geometry_wkb,
    select_canonical_row,
)

_TIMESTAMPS = st.one_of(
    st.none(), st.sampled_from(["2024-01-01T00:00:00Z", "2025-06-01T12:00:00+00:00", "bad"])
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
