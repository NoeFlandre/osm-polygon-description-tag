"""H3 cell assignment, coordinate validation, and antimeridian geometry.

This module owns the coordinate -> H3 mapping and the cell -> ring
geometry helpers used by both the renderer and the dataset-card image.

The defaults are:

* H3 resolution 3, matching the dataset's existing resolution choice;
* the cell id is the string returned by ``h3.latlng_to_cell`` (H3 v4);
* ``cell_to_boundary`` returns ``(lat, lon)`` pairs in H3 v4; this module
  unwraps them to ``(lon, lat)`` for matplotlib's expected ordering.
"""

from __future__ import annotations

import math
from collections.abc import Callable, Sequence
from typing import Final

import h3

DEFAULT_H3_RESOLUTION: Final[int] = 3
MIN_H3_RESOLUTION: Final[int] = 0
MAX_H3_RESOLUTION: Final[int] = 15


class H3PolicyError(ValueError):
    """Raised for invalid coordinates, resolutions, or H3 cell ids."""


def validate_coordinate(lat: float | int | None, lon: float | int | None) -> None:
    """Reject null, non-numeric, non-finite, or out-of-range coordinates.

    Raises :class:`H3PolicyError` with a descriptive message for every
    invalid case. The error message identifies the offending field so
    operators can fix the data without digging through stack traces.
    """
    if lat is None or lon is None:
        raise H3PolicyError("Latitude and longitude must not be null.")
    lat_value, lon_value = _coerce_coordinates(lat, lon)
    _validate_finite_coordinates(lat_value, lon_value)
    _validate_coordinate_ranges(lat_value, lon_value)


def _coerce_coordinates(lat: float | int, lon: float | int) -> tuple[float, float]:
    try:
        return float(lat), float(lon)
    except (TypeError, ValueError) as error:
        raise H3PolicyError(
            f"Latitude and longitude must be numeric; got lat={lat!r}, lon={lon!r}."
        ) from error


def _validate_finite_coordinates(lat: float, lon: float) -> None:
    if not (math.isfinite(lat) and math.isfinite(lon)):
        raise H3PolicyError(f"Latitude and longitude must be finite; got lat={lat!r}, lon={lon!r}.")


def _validate_coordinate_ranges(lat: float, lon: float) -> None:
    if not (-90.0 <= lat <= 90.0):
        raise H3PolicyError(f"Latitude {lat} is outside the [-90, 90] range.")
    if not (-180.0 <= lon <= 180.0):
        raise H3PolicyError(f"Longitude {lon} is outside the [-180, 180] range.")


def _normalize_resolution(resolution: int | None) -> int:
    if not isinstance(resolution, int) or isinstance(resolution, bool):
        raise H3PolicyError(
            f"H3 resolution must be an int in [{MIN_H3_RESOLUTION}, {MAX_H3_RESOLUTION}]; "
            f"got {resolution!r}."
        )
    if not (MIN_H3_RESOLUTION <= resolution <= MAX_H3_RESOLUTION):
        raise H3PolicyError(
            f"H3 resolution must be an int in [{MIN_H3_RESOLUTION}, {MAX_H3_RESOLUTION}]; "
            f"got {resolution!r}."
        )
    return resolution


def assign_h3_cell(
    lat: float | int | None,
    lon: float | int | None,
    *,
    resolution: int = DEFAULT_H3_RESOLUTION,
) -> str:
    """Map a coordinate to its H3 cell id at the requested resolution.

    Raises :class:`H3PolicyError` for invalid coordinates or resolution.
    The returned id is a 15-character hex string for resolution 3 and
    follows the H3 v4 representation for every other valid resolution.
    """
    validate_coordinate(lat, lon)
    normalized = _normalize_resolution(resolution)
    # pragma: no mutate start - validation above rejects either missing coordinate
    assert lat is not None and lon is not None
    # pragma: no mutate end
    try:
        return str(h3.latlng_to_cell(float(lat), float(lon), normalized))
    except (ValueError, h3.H3ValueError) as error:
        raise H3PolicyError(
            f"Could not assign H3 cell for ({lat}, {lon}) at resolution {normalized}: {error}"
        ) from error


def coordinate_to_h3(
    lat: float | int | None,
    lon: float | int | None,
    *,
    resolution: int = DEFAULT_H3_RESOLUTION,
) -> str:
    """Public alias for :func:`assign_h3_cell`."""
    return assign_h3_cell(lat, lon, resolution=resolution)


def split_antimeridian(points: Sequence[tuple[float, float]]) -> list[list[tuple[float, float]]]:
    """Clip an antimeridian-crossing polygon into closed local rings.

    Merely splitting at a longitude jump leaves open fragments. A plotting
    library then closes those fragments with a world-spanning segment. We
    instead unwrap the polygon, clip it against each 360-degree world slab,
    and shift the resulting closed polygons back into ``[-180, 180]``.
    """
    if len(points) < 3:
        return [list(points)]
    if not _crosses_antimeridian(points):
        return [list(points)]

    unwrapped = _unwrap_points(points)
    min_slab, max_slab = _slab_range(unwrapped)
    return _rings_for_slabs(unwrapped, min_slab, max_slab)


def _rings_for_slabs(
    points: Sequence[tuple[float, float]], min_slab: int, max_slab: int
) -> list[list[tuple[float, float]]]:
    rings: list[list[tuple[float, float]]] = []
    for slab in range(min_slab, max_slab + 1):
        clipped = _clip_slab(points, slab)
        if len(clipped) >= 3:
            rings.append([(lon - 360.0 * slab, lat) for lon, lat in clipped])
    return rings


def _crosses_antimeridian(points: Sequence[tuple[float, float]]) -> bool:
    return not all(
        abs(points[index][0] - points[index - 1][0]) <= 180.0 for index in range(len(points))
    )


def _unwrap_points(points: Sequence[tuple[float, float]]) -> list[tuple[float, float]]:
    unwrapped = [points[0]]
    for lon, lat in points[1:]:
        previous_lon = unwrapped[-1][0]
        delta = lon - previous_lon
        if delta > 180.0:  # pragma: no mutate - the boundary computes zero turns
            whole_turns, remainder = divmod(delta - 180.0, 360.0)
            turns = int(whole_turns) + (remainder != 0.0)
            lon = lon - 360.0 * turns
        elif delta < -180.0:  # pragma: no mutate - the boundary computes zero turns
            whole_turns, remainder = divmod(-180.0 - delta, 360.0)
            turns = int(whole_turns) + (remainder != 0.0)
            lon = lon + 360.0 * turns
        unwrapped.append((lon, lat))
    return unwrapped


def _slab_range(points: Sequence[tuple[float, float]]) -> tuple[int, int]:
    longitudes = [lon for lon, _ in points]
    return _slab_index(min(longitudes)), _slab_index(max(longitudes))


def _slab_index(longitude: float) -> int:
    """Return the integer longitude slab containing ``longitude``."""
    return math.floor((longitude + 180.0) / 360.0)


def _clip_slab(points: Sequence[tuple[float, float]], slab: int) -> list[tuple[float, float]]:
    left = -180.0 + 360.0 * slab
    right = 180.0 + 360.0 * slab
    # pragma: no mutate start - None has the same falsey meaning for this bool
    keep_greater = False
    # pragma: no mutate end
    clipped = _clip_longitude(points, left, keep_greater=True)
    # pragma: no mutate start - None has the same falsey meaning for this bool
    return _clip_longitude(
        clipped,
        right,
        keep_greater=keep_greater,
    )
    # pragma: no mutate end


def _clip_longitude(
    points: Sequence[tuple[float, float]],
    boundary: float,
    *,
    keep_greater: bool,
) -> list[tuple[float, float]]:
    """Clip ``points`` against one vertical longitude boundary."""
    if not points:
        return []

    def inside(point: tuple[float, float]) -> bool:
        return point[0] >= boundary if keep_greater else point[0] <= boundary

    def intersection(start: tuple[float, float], end: tuple[float, float]) -> tuple[float, float]:
        delta = end[0] - start[0]
        # pragma: no mutate start - crossings cannot have equal endpoint longitudes
        if delta == 0.0:
            return (boundary, start[1])
        # pragma: no mutate end
        ratio = (boundary - start[0]) / delta
        return (boundary, start[1] + ratio * (end[1] - start[1]))

    output: list[tuple[float, float]] = []
    previous = points[-1]
    previous_inside = inside(previous)
    for current in points:
        current_inside = inside(current)
        output.extend(
            _clip_transition(
                previous,
                current,
                boundary,
                previous_inside,
                current_inside,
                intersection,
            )
        )
        previous = current
        previous_inside = current_inside
    return output


def _clip_transition(
    previous: tuple[float, float],
    current: tuple[float, float],
    boundary: float,
    previous_inside: bool,
    current_inside: bool,
    intersection: Callable[[tuple[float, float], tuple[float, float]], tuple[float, float]],
) -> list[tuple[float, float]]:
    if current_inside:
        if previous_inside:
            return [current]
        return [intersection(previous, current), current]
    if previous_inside:
        return [intersection(previous, current)]
    return []


def cell_rings(cell: str) -> list[list[tuple[float, float]]]:
    """Return the antimeridian-split ``(lon, lat)`` rings for ``cell``.

    The H3 v4 ``cell_to_boundary`` returns ``(lat, lon)`` tuples; this
    helper converts them to ``(lon, lat)`` (matplotlib's expected order)
    and splits antimeridian-crossing rings into closed local fragments
    so a renderer never draws a world-spanning segment.
    """
    try:
        boundary = h3.cell_to_boundary(cell)
    except (ValueError, h3.H3ValueError):
        return []
    if not boundary:
        return []
    raw_points = _boundary_points(boundary)
    return [ring for ring in split_antimeridian(raw_points) if len(ring) >= 3]


def _boundary_points(boundary: Sequence[Sequence[float]]) -> list[tuple[float, float]]:
    points: list[tuple[float, float]] = []
    for pair in boundary:
        if len(pair) >= 2:
            # H3 v4: (lat, lon); flip to (lon, lat) for matplotlib.
            points.append((float(pair[1]), float(pair[0])))
    return points


__all__ = [
    "DEFAULT_H3_RESOLUTION",
    "MAX_H3_RESOLUTION",
    "MIN_H3_RESOLUTION",
    "H3PolicyError",
    "assign_h3_cell",
    "cell_rings",
    "coordinate_to_h3",
    "split_antimeridian",
    "validate_coordinate",
]
