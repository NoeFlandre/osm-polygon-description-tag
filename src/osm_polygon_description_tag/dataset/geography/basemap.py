"""Deterministic Natural Earth landmass overlay for the H3 map.

The land reference is bundled with the package so dataset-card generation is
offline and reproducible.  Rendering keeps the ocean blue and draws the
simplified 110m land polygons beneath the H3 cells, matching the companion
wikidata-only visualisation.
"""

from __future__ import annotations

import json
from collections.abc import Iterator, Sequence
from pathlib import Path
from typing import Any, Final

import matplotlib.patches as mpatches

LAND_BASEMAP_FILENAME: Final[str] = "ne_110m_land.geojson"
_LAND_COLOR: Final[str] = "#e8e0d0"
_LAND_EDGE: Final[str] = "#b8aa90"


def _bundled_basemap_path() -> Path:
    return Path(__file__).parents[2] / "_data" / LAND_BASEMAP_FILENAME


def bundled_basemap_path() -> Path:
    """Return the immutable package path for the bundled land reference."""
    return _bundled_basemap_path()


def load_land_basemap(path: Path | None = None) -> list[Any]:
    """Load bundled Natural Earth land features without network access.

    A missing or malformed asset is treated as an empty overlay so callers
    can still produce a valid ocean-only no-data map; the package build and
    tests separately enforce that the public asset is present and valid.
    """
    candidate = path or _bundled_basemap_path()
    if not candidate.is_file() or candidate.stat().st_size == 0:
        return []
    payload = _read_basemap_payload(candidate)
    return _features_from_payload(payload)


def _read_basemap_payload(path: Path) -> object:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


def _features_from_payload(payload: object) -> list[Any]:
    if not isinstance(payload, dict):
        return []
    features = payload.get("features")
    return list(features) if isinstance(features, list) else []


def _draw_ring(ax: Any, ring: Sequence[Sequence[float]]) -> None:
    if len(ring) < 3:
        return
    try:
        coordinates = [(float(lon), float(lat)) for lon, lat in ring]
    except (TypeError, ValueError):
        return
    ax.add_patch(
        mpatches.Polygon(
            coordinates,
            closed=True,
            facecolor=_LAND_COLOR,
            edgecolor=_LAND_EDGE,
            linewidth=0.2,
            zorder=1,
        )
    )


def draw_landmasses(ax: Any, features: Sequence[Any]) -> None:
    """Draw the outer rings of Natural Earth Polygon/MultiPolygon features."""
    for feature in features:
        _draw_feature(ax, feature)


def _draw_feature(ax: Any, feature: Any) -> None:
    if not isinstance(feature, dict):
        return
    geometry = feature.get("geometry")
    if not isinstance(geometry, dict):
        return
    geometry_type = geometry.get("type")
    coordinates = geometry.get("coordinates")
    if geometry_type == "Polygon":
        _draw_polygon(ax, coordinates)
    elif geometry_type == "MultiPolygon":
        _draw_multipolygon(ax, coordinates)


def _draw_polygon(ax: Any, coordinates: Any) -> None:
    if isinstance(coordinates, list) and coordinates:
        _draw_ring(ax, coordinates[0])


def _outer_rings(coordinates: Any) -> Iterator[Any]:
    """Yield the first ring from each valid MultiPolygon member."""
    if not isinstance(coordinates, list):
        return
    for polygon in coordinates:
        if isinstance(polygon, list) and polygon:
            yield polygon[0]


def _draw_multipolygon(ax: Any, coordinates: Any) -> None:
    for ring in _outer_rings(coordinates):
        _draw_ring(ax, ring)


__all__ = [
    "LAND_BASEMAP_FILENAME",
    "bundled_basemap_path",
    "draw_landmasses",
    "load_land_basemap",
]
