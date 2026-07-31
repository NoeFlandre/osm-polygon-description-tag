"""Deterministic Natural Earth landmass overlay for the H3 map.

The land reference is bundled with the package so dataset-card generation is
offline and reproducible.  Rendering keeps the ocean blue and draws the
simplified 110m land polygons beneath the H3 cells, matching the companion
wikidata-only visualisation.
"""

from __future__ import annotations

import json
from collections.abc import Sequence
from pathlib import Path
from typing import Any, Final

import matplotlib.patches as mpatches

LAND_BASEMAP_FILENAME: Final[str] = "ne_110m_land.geojson"
_LAND_COLOR: Final[str] = "#e8e0d0"
_LAND_EDGE: Final[str] = "#b8aa90"


def _bundled_basemap_path() -> Path:
    return Path(__file__).parents[2] / "_data" / LAND_BASEMAP_FILENAME


def load_land_basemap(path: Path | None = None) -> list[Any]:
    """Load bundled Natural Earth land features without network access.

    A missing or malformed asset is treated as an empty overlay so callers
    can still produce a valid ocean-only no-data map; the package build and
    tests separately enforce that the public asset is present and valid.
    """
    candidate = path or _bundled_basemap_path()
    if not candidate.is_file() or candidate.stat().st_size == 0:
        return []
    try:
        payload = json.loads(candidate.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return []
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
        if not isinstance(feature, dict):
            continue
        geometry = feature.get("geometry")
        if not isinstance(geometry, dict):
            continue
        geometry_type = geometry.get("type")
        coordinates = geometry.get("coordinates")
        if geometry_type == "Polygon" and isinstance(coordinates, list) and coordinates:
            _draw_ring(ax, coordinates[0])
        elif geometry_type == "MultiPolygon" and isinstance(coordinates, list):
            for polygon in coordinates:
                if isinstance(polygon, list) and polygon:
                    _draw_ring(ax, polygon[0])


__all__ = [
    "LAND_BASEMAP_FILENAME",
    "draw_landmasses",
    "load_land_basemap",
]
