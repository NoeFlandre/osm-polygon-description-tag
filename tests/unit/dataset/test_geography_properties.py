"""Coordinate validation, H3 assignment, antimeridian clipping and area buckets."""

from __future__ import annotations

import math
from itertools import pairwise

import h3
import pyarrow as pa
import pytest
from hypothesis import assume, given
from hypothesis import strategies as st

from osm_polygon_description_tag.dataset.geography import area_histogram
from osm_polygon_description_tag.dataset.geography.h3_policy import (
    H3PolicyError,
    assign_h3_cell,
    split_antimeridian,
    validate_coordinate,
)

_ANY_FLOAT = st.floats(allow_nan=True, allow_infinity=True)
_LAT = st.floats(min_value=-90, max_value=90, allow_nan=False)
_LON = st.floats(min_value=-180, max_value=180, allow_nan=False)


@given(_ANY_FLOAT, _ANY_FLOAT)
def test_a_coordinate_is_valid_exactly_when_finite_and_in_range(lat: float, lon: float) -> None:
    in_range = math.isfinite(lat) and math.isfinite(lon) and abs(lat) <= 90 and abs(lon) <= 180

    if in_range:
        validate_coordinate(lat, lon)
    else:
        with pytest.raises(H3PolicyError):
            validate_coordinate(lat, lon)


@given(_LAT, _LON, st.integers(min_value=0, max_value=9))
def test_an_assigned_cell_has_the_requested_resolution_and_contains_its_centre(
    lat: float, lon: float, resolution: int
) -> None:
    cell = assign_h3_cell(lat, lon, resolution=resolution)
    centre_lat, centre_lon = h3.cell_to_latlng(cell)

    assert h3.get_resolution(cell) == resolution
    assert assign_h3_cell(centre_lat, centre_lon, resolution=resolution) == cell


_RING_POINTS = st.lists(
    st.tuples(st.floats(min_value=-180, max_value=180), st.floats(min_value=-80, max_value=80)),
    min_size=3,
    max_size=8,
)


def _unwrapped_extent(points: list[tuple[float, float]]) -> float:
    """Longitude extent of a ring after each step is wrapped into (-180, 180]."""
    lon = points[0][0]
    longitudes = [lon]
    for (previous, _), (current, _) in pairwise(points):
        step = (current - previous + 180.0) % 360.0 - 180.0
        lon += step
        longitudes.append(lon)
    return max(longitudes) - min(longitudes)


@given(_RING_POINTS)
def test_clipped_rings_stay_inside_the_world_and_no_edge_spans_half_of_it(
    points: list[tuple[float, float]],
) -> None:
    # A ring wider than half the world has an edge that legitimately spans more than
    # 180 degrees once clipped, so the edge bound only holds for narrower ones. The
    # vertex count can shrink (rings under three vertices are dropped), so it is
    # not asserted.
    assume(_unwrapped_extent(points) <= 180.0)
    rings = split_antimeridian(points)

    for ring in rings:
        longitudes = [lon for lon, _ in ring]
        assert all(-180.0 - 1e-9 <= lon <= 180.0 + 1e-9 for lon in longitudes)
        edges = zip(longitudes, [*longitudes[1:], longitudes[0]], strict=True)
        assert all(abs(end - start) <= 180.0 + 1e-9 for start, end in edges)


@given(st.lists(st.floats(min_value=0, max_value=1e12, allow_nan=False), min_size=1, max_size=40))
def test_area_buckets_are_monotone_in_area(areas: list[float]) -> None:
    indices = [area_histogram._bucket_index(area) for area in sorted(areas)]

    assert indices == sorted(indices)
    assert all(0 <= index < area_histogram.AREA_BUCKET_COUNT for index in indices)


@given(
    st.lists(
        st.one_of(st.none(), st.floats(min_value=0, max_value=1e12, allow_nan=False)), max_size=60
    ),
    st.randoms(use_true_random=False),
)
def test_bucket_counts_total_the_non_null_areas_whatever_their_order(
    areas: list[float | None], rng: object
) -> None:
    shuffled = list(areas)
    rng.shuffle(shuffled)  # type: ignore[attr-defined]
    first = [0] * area_histogram.AREA_BUCKET_COUNT
    second = [0] * area_histogram.AREA_BUCKET_COUNT

    area_histogram._add_bucket_counts(first, pa.array(areas, type=pa.float64()))
    area_histogram._add_bucket_counts(second, pa.array(shuffled, type=pa.float64()))

    assert first == second
    assert sum(first) == sum(area is not None for area in areas)
    assume(True)
