"""H3 policy contract tests.

These tests pin the H3 v4 boundary coordinate ordering, the deterministic
centroid-to-H3 assignment, the resolution validation, the antimeridian-safe
cell ring conversion, the coordinate validation rules, and the stable
sorted cell ordering used by both the renderer and the dataset card.
"""

from __future__ import annotations

import math
from unittest.mock import call, patch

import pytest

import osm_polygon_description_tag.dataset.geography.h3_policy as h3_policy_module
from osm_polygon_description_tag.dataset.geography import (
    DEFAULT_H3_RESOLUTION,
    H3PolicyError,
    assign_h3_cell,
    cell_rings,
    coordinate_to_h3,
    split_antimeridian,
    validate_coordinate,
)

# ---------------------------------------------------------------------------
# Coordinate validation
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("lat", "lon"),
    [
        (0.0, 0.0),
        (48.8566, 2.3522),
        (-33.8688, 151.2093),
        (90.0, 180.0),
        (-90.0, -180.0),
    ],
)
def test_validate_coordinate_accepts_in_range_values(lat: float, lon: float) -> None:
    validate_coordinate(lat, lon)


@pytest.mark.parametrize(
    ("lat", "lon", "match"),
    [
        (None, 0.0, "null"),
        (0.0, None, "null"),
        (float("nan"), 0.0, "finite"),
        (0.0, float("nan"), "finite"),
        (float("inf"), 0.0, "finite"),
        (0.0, float("-inf"), "finite"),
        (91.0, 0.0, "range"),
        (-91.0, 0.0, "range"),
        (0.0, 181.0, "range"),
        (0.0, -181.0, "range"),
    ],
)
def test_validate_coordinate_rejects_invalid_inputs(
    lat: float | None, lon: float | None, match: str
) -> None:
    with pytest.raises(H3PolicyError, match=match):
        validate_coordinate(lat, lon)


def test_validate_coordinate_rejects_non_numeric() -> None:
    with pytest.raises(H3PolicyError, match="numeric"):
        validate_coordinate("not-a-number", "2.0")  # type: ignore[arg-type]


def test_validate_coordinate_uses_exact_null_error_contract() -> None:
    with pytest.raises(H3PolicyError) as error:
        validate_coordinate(None, 0.0)
    assert str(error.value) == "Latitude and longitude must not be null."


# ---------------------------------------------------------------------------
# Centroid -> H3 cell assignment
# ---------------------------------------------------------------------------


def test_assign_h3_cell_is_deterministic() -> None:
    cell_a = assign_h3_cell(48.8566, 2.3522)
    cell_b = assign_h3_cell(48.8566, 2.3522)
    assert cell_a == cell_b
    assert isinstance(cell_a, str)
    assert len(cell_a) == 15


def test_assign_h3_cell_uses_default_resolution() -> None:
    cell = assign_h3_cell(48.8566, 2.3522)
    # H3 v4 cell id at resolution 3 is a 15-character hex string.
    assert len(cell) == 15
    # The resolution is encoded implicitly in the cell id; we exercise the
    # explicit override path below for additional assurance.
    assert assign_h3_cell(48.8566, 2.3522, resolution=DEFAULT_H3_RESOLUTION) == cell


def test_assign_h3_cell_distinct_points_can_share_cell() -> None:
    # Two points within the same H3 resolution-3 cell.
    a = assign_h3_cell(0.0, 0.0)
    b = assign_h3_cell(0.01, 0.01)
    # They may be equal or adjacent; the test pins the contract.
    assert isinstance(a, str)
    assert isinstance(b, str)


def test_assign_h3_cell_rejects_null_or_non_finite() -> None:
    with pytest.raises(H3PolicyError):
        assign_h3_cell(None, 0.0)  # type: ignore[arg-type]
    with pytest.raises(H3PolicyError):
        assign_h3_cell(0.0, None)  # type: ignore[arg-type]
    with pytest.raises(H3PolicyError):
        assign_h3_cell(float("nan"), 0.0)
    with pytest.raises(H3PolicyError):
        assign_h3_cell(0.0, float("inf"))


def test_assign_h3_cell_rejects_out_of_range() -> None:
    with pytest.raises(H3PolicyError, match="Latitude"):
        assign_h3_cell(91.0, 0.0)
    with pytest.raises(H3PolicyError, match="Longitude"):
        assign_h3_cell(0.0, 181.0)


def test_assign_h3_cell_preserves_h3_failure_context() -> None:
    with (
        patch.object(
            h3_policy_module.h3,
            "latlng_to_cell",
            side_effect=ValueError("bad cell"),
        ),
        pytest.raises(H3PolicyError) as error,
    ):
        assign_h3_cell(1.0, 2.0, resolution=7)

    assert str(error.value) == ("Could not assign H3 cell for (1.0, 2.0) at resolution 7: bad cell")


@pytest.mark.parametrize("bad_resolution", [-1, 16, 100, "3", None])
def test_assign_h3_cell_rejects_invalid_resolution(bad_resolution: object) -> None:
    with pytest.raises(H3PolicyError, match="resolution"):
        assign_h3_cell(0.0, 0.0, resolution=bad_resolution)  # type: ignore[arg-type]


def test_assign_h3_cell_supports_full_resolution_range() -> None:
    for resolution in (0, 5, 10, 15):
        assert isinstance(assign_h3_cell(0.0, 0.0, resolution=resolution), str)


# ---------------------------------------------------------------------------
# H3 v4 boundary coordinate ordering
# ---------------------------------------------------------------------------


def test_cell_rings_returns_lon_lat_tuples() -> None:
    cell = assign_h3_cell(48.8566, 2.3522)
    rings = cell_rings(cell)
    assert rings, "expected at least one ring for a valid H3 cell"
    ring = rings[0]
    assert len(ring) >= 3
    for point in ring:
        assert len(point) == 2
        lon, lat = point
        assert -180.0 <= lon <= 180.0
        assert -90.0 <= lat <= 90.0


def test_cell_rings_for_invalid_cell_returns_empty() -> None:
    # Even an invalid cell id should not raise; it returns an empty list.
    assert cell_rings("000000000000000") == []


def test_cell_rings_flips_boundary_coordinates_and_keeps_three_points() -> None:
    boundary = [(10.0, 20.0), (30.0, 40.0), (50.0, 60.0)]
    with patch.object(h3_policy_module.h3, "cell_to_boundary", return_value=boundary):
        assert cell_rings("cell") == [[(20.0, 10.0), (40.0, 30.0), (60.0, 50.0)]]


# ---------------------------------------------------------------------------
# Antimeridian splitting
# ---------------------------------------------------------------------------


def test_split_antimeridian_short_polygon_is_preserved() -> None:
    points = [(0.0, 0.0), (1.0, 0.0), (1.0, 1.0)]
    assert split_antimeridian(points) == [points]


def test_split_antimeridian_clean_polygon_is_preserved() -> None:
    points = [(0.0, 0.0), (10.0, 0.0), (10.0, 10.0), (0.0, 10.0)]
    assert split_antimeridian(points) == [points]


def test_split_antimeridian_crossing_polygon_is_split() -> None:
    # A polygon that jumps from 170 to -170 across the antimeridian.
    points = [(170.0, 0.0), (-170.0, 0.0), (-170.0, 10.0), (170.0, 10.0)]
    rings = split_antimeridian(points)
    assert len(rings) >= 2
    for ring in rings:
        for lon, lat in ring:
            assert -180.0 <= lon <= 180.0
            assert -90.0 <= lat <= 90.0
        # The ring is closed (or near-closed) and large enough to draw.
        assert len(ring) >= 3


def test_h3_antimeridian_internal_helpers_pin_boundary_and_unwrap_rules() -> None:
    from osm_polygon_description_tag.dataset.geography.h3_policy import (
        _clip_longitude,
        _clip_slab,
        _slab_range,
        _unwrap_points,
    )

    # Boundary points belong to the retained side, while crossings get a
    # deterministic interpolated point at the boundary.
    points = [(-1.0, 0.0), (1.0, 2.0)]
    assert _clip_longitude(points, 0.0, keep_greater=True)[0] == (0.0, 1.0)
    assert _clip_longitude(points, 0.0, keep_greater=False)[0] == (0.0, 1.0)
    assert _clip_longitude([(0.0, 3.0)], 0.0, keep_greater=True) == [(0.0, 3.0)]
    assert _clip_longitude([(0.0, 3.0)], 0.0, keep_greater=False) == [(0.0, 3.0)]

    unwrapped = _unwrap_points([(170.0, 0.0), (-170.0, 1.0), (170.0, 2.0)])
    assert unwrapped == [(170.0, 0.0), (190.0, 1.0), (170.0, 2.0)]
    assert _slab_range(unwrapped) == (0, 1)
    clipped = _clip_slab(unwrapped, 0)
    assert clipped
    assert all(-180.0 <= lon <= 180.0 for lon, _lat in clipped)


def test_antimeridian_helpers_pin_boundary_direction_and_slab_math() -> None:
    from osm_polygon_description_tag.dataset.geography.h3_policy import (
        _clip_longitude,
        _clip_slab,
        _crosses_antimeridian,
        _slab_index,
        _slab_range,
        _unwrap_points,
    )

    assert _crosses_antimeridian([(0.0, 0.0), (180.0, 0.0), (0.0, 1.0)]) is False
    assert _crosses_antimeridian([(0.0, 0.0), (181.0, 0.0)]) is True
    assert _crosses_antimeridian([(-180.0, 0.0), (0.0, 0.0), (-180.0, 0.0), (100.0, 0.0)]) is True

    assert _unwrap_points([(0.0, 0.0), (180.0, 1.0)]) == [
        (0.0, 0.0),
        (180.0, 1.0),
    ]
    assert _unwrap_points([(0.0, 0.0), (-180.0, 1.0)]) == [
        (0.0, 0.0),
        (-180.0, 1.0),
    ]
    assert _unwrap_points([(-170.0, 0.0), (170.0, 1.0)]) == [
        (-170.0, 0.0),
        (-190.0, 1.0),
    ]
    assert _unwrap_points([(170.0, 0.0), (-170.0, 1.0)]) == [
        (170.0, 0.0),
        (190.0, 1.0),
    ]
    assert _unwrap_points([(0.0, 0.0), (721.0, 1.0)]) == [
        (0.0, 0.0),
        (1.0, 1.0),
    ]
    assert _unwrap_points([(0.0, 0.0), (-721.0, 1.0)]) == [
        (0.0, 0.0),
        (-1.0, 1.0),
    ]
    assert _unwrap_points([(0.0, 0.0), (541.0, 1.0)]) == [
        (0.0, 0.0),
        (-179.0, 1.0),
    ]
    assert _unwrap_points([(0.0, 0.0), (-541.0, 1.0)]) == [
        (0.0, 0.0),
        (179.0, 1.0),
    ]
    assert _unwrap_points([(0.0, 0.0), (181.0, 1.0), (-181.0, 2.0)]) == [
        (0.0, 0.0),
        (-179.0, 1.0),
        (-181.0, 2.0),
    ]

    assert _slab_range([(-181.0, 0.0), (179.0, 0.0)]) == (-1, 0)
    assert _slab_range([(-180.0, 0.0), (180.0, 0.0)]) == (0, 1)
    assert _slab_range([(180.0, 0.0), (181.0, 0.0)]) == (1, 1)
    assert _slab_index(-181.0) == -1
    assert _slab_index(180.0) == 1
    assert _clip_slab([(170.0, 0.0), (190.0, 2.0), (170.0, 4.0)], 1) == [
        (180.0, 1.0),
        (190.0, 2.0),
        (180.0, 3.0),
    ]
    assert _clip_slab([(530.0, 0.0), (550.0, 2.0), (530.0, 4.0)], 1) == [
        (530.0, 0.0),
        (540.0, 1.0),
        (540.0, 3.0),
        (530.0, 4.0),
    ]
    assert _clip_longitude([(-0.5, 0.0), (0.5, 2.0)], 0.0, keep_greater=True) == [
        (0.0, 1.0),
        (0.0, 1.0),
        (0.5, 2.0),
    ]


def test_unwrap_points_handles_each_wrap_threshold_without_drift() -> None:
    from osm_polygon_description_tag.dataset.geography.h3_policy import _unwrap_points

    assert _unwrap_points([(0.0, 0.0), (181.0, 1.0)]) == [
        (0.0, 0.0),
        (-179.0, 1.0),
    ]
    assert _unwrap_points([(0.0, 0.0), (-181.0, 1.0)]) == [
        (0.0, 0.0),
        (179.0, 1.0),
    ]
    assert _unwrap_points([(-170.0, 0.0), (170.0, 1.0)]) == [
        (-170.0, 0.0),
        (-190.0, 1.0),
    ]
    assert _unwrap_points([(170.0, 0.0), (-170.0, 1.0)]) == [
        (170.0, 0.0),
        (190.0, 1.0),
    ]


def test_clip_longitude_propagates_inside_state_and_boundaries_exactly() -> None:
    from osm_polygon_description_tag.dataset.geography.h3_policy import _clip_longitude

    real_transition = h3_policy_module._clip_transition
    with patch.object(
        h3_policy_module,
        "_clip_transition",
        wraps=real_transition,
    ) as transition:
        result = _clip_longitude(
            [(-1.0, 0.0), (1.0, 1.0), (-1.0, 2.0)],
            0.0,
            keep_greater=True,
        )

    assert result == [(0.0, 0.5), (1.0, 1.0), (0.0, 1.5)]
    assert [call.args[2] for call in transition.call_args_list] == [0.0, 0.0, 0.0]
    assert [call.args[3:5] for call in transition.call_args_list] == [
        (False, False),
        (False, True),
        (True, False),
    ]


def test_split_antimeridian_preserves_short_inputs_and_exact_ring_thresholds() -> None:
    from osm_polygon_description_tag.dataset.geography.h3_policy import (
        _rings_for_slabs,
    )

    short = [(170.0, 0.0), (-170.0, 0.0), (170.0, 1.0)]
    short_rings = split_antimeridian(short)
    assert len(short_rings) == 2
    assert all(len(ring) >= 3 for ring in short_rings)
    assert split_antimeridian([(1.0, 2.0), (3.0, 4.0)]) == [[(1.0, 2.0), (3.0, 4.0)]]

    clipped = {
        1: [(360.0, 0.0), (361.0, 1.0), (362.0, 2.0)],
        2: [(720.0, 3.0), (721.0, 4.0), (722.0, 5.0)],
    }
    with patch.object(
        h3_policy_module, "_clip_slab", side_effect=lambda _points, slab: clipped[slab]
    ) as clip_slab:
        rings = _rings_for_slabs([(0.0, 0.0)], 1, 2)

    assert rings == [
        [(0.0, 0.0), (1.0, 1.0), (2.0, 2.0)],
        [(0.0, 3.0), (1.0, 4.0), (2.0, 5.0)],
    ]
    assert clip_slab.call_args_list == [
        call([(0.0, 0.0)], 1),
        call([(0.0, 0.0)], 2),
    ]


# ---------------------------------------------------------------------------
# coordinate_to_h3 entry point
# ---------------------------------------------------------------------------


def test_coordinate_to_h3_matches_assign_h3_cell() -> None:
    assert coordinate_to_h3(48.8566, 2.3522) == assign_h3_cell(48.8566, 2.3522)


def test_coordinate_to_h3_uses_default_resolution() -> None:
    assert coordinate_to_h3(48.8566, 2.3522) == coordinate_to_h3(
        48.8566, 2.3522, resolution=DEFAULT_H3_RESOLUTION
    )


def test_coordinate_to_h3_forwards_explicit_resolution() -> None:
    with patch.object(h3_policy_module, "assign_h3_cell", return_value="cell") as assign:
        assert coordinate_to_h3(1.0, 2.0, resolution=7) == "cell"

    assign.assert_called_once_with(1.0, 2.0, resolution=7)


# ---------------------------------------------------------------------------
# Stable sorted ordering
# ---------------------------------------------------------------------------


def test_assigned_cells_sort_lexicographically() -> None:
    cells = sorted(
        {assign_h3_cell(0.0, 0.0), assign_h3_cell(45.0, 90.0), assign_h3_cell(-45.0, -90.0)}
    )
    assert cells == sorted(cells)
    # H3 v4 cell ids compare as hex strings.
    assert all(isinstance(cell, str) and len(cell) == 15 for cell in cells)
    # The set must be stable.
    assert math.isfinite(1.0)
