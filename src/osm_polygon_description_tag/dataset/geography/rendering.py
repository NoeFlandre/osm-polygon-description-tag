"""Deterministic PNG rendering of the H3 density map.

The renderer is a pure function of the H3 cell counts, the bundled Natural
Earth land reference, and the chosen output path. It never downloads
basemap data and produces a self-contained world map with stable visual
constants for byte-for-byte determinism.
"""

from __future__ import annotations

import os
import tempfile
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Final

import matplotlib

matplotlib.use("Agg")  # non-interactive backend for CI/macOS terminal runs

import matplotlib.colors as mcolors
import matplotlib.patches as mpatches
import matplotlib.pyplot as plt
import matplotlib.ticker as mtick

from osm_polygon_description_tag.dataset.geography.basemap import (
    _LAND_COLOR,
    _LAND_EDGE,
    draw_landmasses,
    load_land_basemap,
)
from osm_polygon_description_tag.dataset.geography.h3_policy import (
    cell_rings,
)

# Shared world-extent visual constants.
_OCEAN_COLOR: Final[str] = "#cfe2f3"
_GRID_COLOR: Final[str] = "#ffffff"
_GRID_LAT_EVERY: Final[int] = 30
_GRID_LON_EVERY: Final[int] = 60

# Shared figure layout constants.
_FIGSIZE: Final[tuple[float, float]] = (16.0, 8.0)
_DPI: Final[int] = 100

# Polygon-count-only visual constants.
_COLORMAP_NAME: Final[str] = "magma"
_COUNT_ALPHA: Final[float] = 0.95
_EDGE_COLOR: Final[str] = "#333333"
_EDGE_WIDTH: Final[float] = 0.2
_TICK_LABELSIZE: Final[int] = 7
_TITLE_FONTSIZE: Final[int] = 14
_CAPTION_FONTSIZE: Final[int] = 7
_COLORBAR_FRACTION: Final[float] = 0.025
_COLORBAR_PAD: Final[float] = 0.02

# PNG metadata.
_METADATA_SOFTWARE: Final[str] = "osm-polygon-description-tag"

# Caption templates.
_TITLE: Final[str] = "H3 Density of Description-Tagged Polygons"
_NO_DATA_CAPTION: Final[str] = (
    "H3 density of description-tagged polygons. 0 polygons across 0 H3 cells (no data)."
)


@dataclass(frozen=True)
class RenderResult:
    """Outcome of a render function.

    The PNG is written to ``output_path`` and the exact caption text
    rendered onto the figure is exposed here so callers and tests can
    introspect it without parsing the rasterized image.
    """

    output_path: Path
    caption: str


def _init_axes(ax: Any) -> None:
    """Apply the shared world-extent styling used by every map."""
    ax.set_facecolor(_OCEAN_COLOR)
    ax.set_xlim(-180.0, 180.0)
    ax.set_ylim(-90.0, 90.0)
    ax.set_xticks(range(-180, 181, _GRID_LON_EVERY))
    ax.set_yticks(range(-90, 91, _GRID_LAT_EVERY))
    ax.grid(True, color=_GRID_COLOR, linewidth=0.3, alpha=0.4)
    ax.tick_params(colors="#666666", labelsize=_TICK_LABELSIZE)
    ax.set_aspect("equal", adjustable="box")


def _format_count_tick(value: float, _position: int | None = None) -> str:
    """Format a polygon-count colorbar value as a human-readable integer label."""
    count = round(value)
    if count < 1_000:
        return str(count)
    if count < 1_000_000:
        thousands = count / 1_000.0
        return f"{thousands:.0f}k" if thousands.is_integer() else f"{thousands:.1f}k"
    millions = count / 1_000_000.0
    return f"{millions:.1f}M"


def _build_caption(
    cells: Mapping[str, int],
    total_rows: int,
    occupied_cells: int,
) -> str:
    if total_rows == 0 or occupied_cells == 0:
        return _NO_DATA_CAPTION
    return (
        "H3 density of description-tagged polygons. "
        f"Each dataset row is counted exactly once (regional overlap retained). "
        f"{total_rows:,} polygons across {occupied_cells:,} H3 cells at "
        "resolution 3 on a logarithmic colour scale."
    )


def _safe_counts(cells: Mapping[str, int]) -> list[int]:
    """Return cell counts as a list of positive integers."""
    return [max(int(value), 1) for value in cells.values()]


def _draw_cell(
    ax: Any,
    cell: str,
    *,
    count: int,
    cmap: mcolors.Colormap,
    norm: mcolors.LogNorm,
) -> None:
    """Draw a single H3 cell on ``ax`` for the density map."""
    facecolor: Any = cmap(norm(max(int(count), 1)))
    for ring in cell_rings(cell):
        if len(ring) < 3:
            continue
        patch = mpatches.Polygon(
            ring,
            closed=True,
            facecolor=facecolor,
            edgecolor=_EDGE_COLOR,
            linewidth=_EDGE_WIDTH,
            alpha=_COUNT_ALPHA,
            zorder=3,
        )
        ax.add_patch(patch)


def _atomic_save_png(fig: Any, output_path: Path) -> None:
    """Save ``fig`` to ``output_path`` via a temporary file then atomic rename.

    Guarantees:

    * The temporary file is created in the same directory as
      ``output_path`` so :func:`os.replace` is atomic on the same
      filesystem.
    * The temporary file is ``fsync``-ed before the rename so the bytes
      survive a crash mid-write.
    * When the destination already exists and is byte-identical to the
      freshly rendered PNG, the existing file is preserved so the mtime
      and inode are untouched.
    * When the destination differs, :func:`os.replace` performs the
      atomic swap and the parent directory is ``fsync``-ed so the
      rename is durable.
    * The temporary file is removed on every code path, including the
      byte-identical short-circuit and the catch-all failure path.
    """
    output_path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(
        prefix=f".{output_path.name}.",
        suffix=".tmp",
        dir=str(output_path.parent),
    )
    os.close(fd)
    tmp_path = Path(tmp_name)
    try:
        fig.savefig(
            str(tmp_path),
            format="png",
            facecolor="white",
            metadata={"Software": _METADATA_SOFTWARE},
        )
        with open(tmp_path, "rb") as handle:
            os.fsync(handle.fileno())
        if output_path.exists():
            existing_bytes = output_path.read_bytes()
            new_bytes = tmp_path.read_bytes()
            if existing_bytes == new_bytes:
                # Byte-identical regeneration; preserve the existing
                # file's mtime and inode.
                tmp_path.unlink(missing_ok=True)
                return
        os.replace(tmp_path, output_path)
        dir_fd = os.open(str(output_path.parent), os.O_RDONLY)
        try:
            os.fsync(dir_fd)
        finally:
            os.close(dir_fd)
    except BaseException:
        tmp_path.unlink(missing_ok=True)
        raise


def render_density_map(
    cells: Mapping[str, int],
    output_path: Path,
    *,
    land_features: Sequence[Any] | None = None,
) -> RenderResult:
    """Render the H3 density map and atomically write it to ``output_path``.

    The ``cells`` argument maps each H3 cell id to the number of dataset
    rows whose geometry centroid falls inside that cell. The rendered
    caption reports the total row count and the number of occupied
    cells, derived from the aggregation. If ``land_features`` is omitted,
    the bundled Natural Earth 110m land reference is loaded. Passing an
    explicit sequence is useful for tests and alternate callers; passing an
    empty sequence intentionally renders ocean only. Identical inputs and
    the same bundled reference produce byte-identical PNGs.
    """
    if not cells and not output_path.exists():
        # Render the no-data image even when no file exists yet.
        pass

    sorted_cells: Sequence[tuple[str, int]] = tuple(sorted(cells.items()))
    total_rows = sum(int(value) for value in cells.values())
    occupied_cells = len(sorted_cells)
    caption = _build_caption(cells, total_rows, occupied_cells)

    fig, ax = plt.subplots(figsize=_FIGSIZE, dpi=_DPI)
    fig.set_facecolor("white")
    _init_axes(ax)
    if land_features is None:
        land_features = load_land_basemap()
    if land_features:
        draw_landmasses(ax, land_features)

    cmap = plt.get_cmap(_COLORMAP_NAME)
    if sorted_cells:
        counts = _safe_counts(cells)
        minimum = min(counts)
        maximum = max(max(counts), minimum + 1)
        # LogNorm requires vmin < vmax; the guard above guarantees this even
        # for the one-cell case.
        norm = mcolors.LogNorm(vmin=minimum, vmax=maximum)
        for cell, count in sorted_cells:
            _draw_cell(ax, cell, count=count, cmap=cmap, norm=norm)

        sm = plt.cm.ScalarMappable(cmap=cmap, norm=norm)
        sm.set_array([])
        colorbar = fig.colorbar(sm, ax=ax, fraction=_COLORBAR_FRACTION, pad=_COLORBAR_PAD)
        colorbar.set_label(
            "Polygons per H3 cell (log scale)",
            fontsize=8,
            color="#333333",
        )
        colorbar.ax.yaxis.set_major_formatter(mtick.FuncFormatter(_format_count_tick))
        colorbar.ax.tick_params(labelsize=_TICK_LABELSIZE)
    else:
        # No cells: still add an empty colorbar to keep layout stable.
        norm = mcolors.LogNorm(vmin=1, vmax=2)
        sm = plt.cm.ScalarMappable(cmap=cmap, norm=norm)
        sm.set_array([])
        fig.colorbar(sm, ax=ax, fraction=_COLORBAR_FRACTION, pad=_COLORBAR_PAD)

    fig.suptitle(_TITLE, fontsize=_TITLE_FONTSIZE, color="#222222", y=0.98)
    fig.text(
        0.5,
        0.02,
        caption,
        ha="center",
        va="bottom",
        fontsize=_CAPTION_FONTSIZE,
        color="#444444",
        wrap=True,
    )

    try:
        fig.tight_layout(rect=(0, 0.06, 1, 0.95))
        _atomic_save_png(fig, output_path)
    finally:
        plt.close(fig)

    return RenderResult(output_path=output_path, caption=caption)


def atomic_save_png_for_testing(
    fig: Any,
    output_path: Path,
) -> None:
    """Test-only re-export of :func:`_atomic_save_png` for failure-path coverage."""
    _atomic_save_png(fig, output_path)


__all__ = [
    "_COLORMAP_NAME",
    "_DPI",
    "_FIGSIZE",
    "_LAND_COLOR",
    "_LAND_EDGE",
    "_METADATA_SOFTWARE",
    "_NO_DATA_CAPTION",
    "RenderResult",
    "render_density_map",
]
