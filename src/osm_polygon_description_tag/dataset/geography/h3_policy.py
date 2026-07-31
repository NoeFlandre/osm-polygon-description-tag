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
from collections.abc import Sequence
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
    try:
        lat_value = float(lat)
        lon_value = float(lon)
    except (TypeError, ValueError) as error:
        raise H3PolicyError(
            f"Latitude and longitude must be numeric; got lat={lat!r}, lon={lon!r}."
        ) from error
    if not (math.isfinite(lat_value) and math.isfinite(lon_value)):
        raise H3PolicyError(
            f"Latitude and longitude must be finite; got lat={lat_value!r}, lon={lon_value!r}."
        )
    if not (-90.0 <= lat_value <= 90.0):
        raise H3PolicyError(f"Latitude {lat_value} is outside the [-90, 90] range.")
    if not (-180.0 <= lon_value <= 180.0):
        raise H3PolicyError(f"Longitude {lon_value} is outside the [-180, 180] range.")


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
    assert lat is not None and lon is not None
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
    if all(abs(points[index][0] - points[index - 1][0]) <= 180.0 for index in range(len(points))):
        return [list(points)]

    unwrapped = [points[0]]
    for lon, lat in points[1:]:
        previous_lon = unwrapped[-1][0]
        while lon - previous_lon > 180.0:
            lon -= 360.0
        while lon - previous_lon < -180.0:
            lon += 360.0
        unwrapped.append((lon, lat))

    min_slab = math.floor((min(lon for lon, _ in unwrapped) + 180.0) / 360.0)
    max_slab = math.floor((max(lon for lon, _ in unwrapped) + 180.0) / 360.0)
    rings: list[list[tuple[float, float]]] = []
    for slab in range(min_slab, max_slab + 1):
        left = -180.0 + 360.0 * slab
        right = 180.0 + 360.0 * slab
        clipped = _clip_longitude(
            _clip_longitude(unwrapped, left, keep_greater=True),
            right,
            keep_greater=False,
        )
        if len(clipped) >= 3:
            rings.append([(lon - 360.0 * slab, lat) for lon, lat in clipped])
    return rings


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
        if delta == 0.0:
            return (boundary, start[1])
        ratio = (boundary - start[0]) / delta
        return (boundary, start[1] + ratio * (end[1] - start[1]))

    output: list[tuple[float, float]] = []
    previous = points[-1]
    previous_inside = inside(previous)
    for current in points:
        current_inside = inside(current)
        if current_inside:
            if not previous_inside:
                output.append(intersection(previous, current))
            output.append(current)
        elif previous_inside:
            output.append(intersection(previous, current))
        previous = current
        previous_inside = current_inside
    return output


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
    raw_points: list[tuple[float, float]] = []
    for pair in boundary:
        if len(pair) >= 2:
            # H3 v4: (lat, lon); flip to (lon, lat) for matplotlib.
            raw_points.append((float(pair[1]), float(pair[0])))
    return [ring for ring in split_antimeridian(raw_points) if len(ring) >= 3]


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
