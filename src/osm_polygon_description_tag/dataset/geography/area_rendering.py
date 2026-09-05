"""Deterministic PNG rendering of the area distribution histogram.

The renderer is a pure function of the bucket counts and the chosen
output path. It never downloads anything and produces a self-contained
horizontal bar chart with stable visual constants for byte-for-byte
determinism.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Final

import matplotlib

matplotlib.use("Agg")  # non-interactive backend for CI/macOS terminal runs

import matplotlib.pyplot as plt
import numpy as np

from osm_polygon_description_tag.dataset.geography.area_histogram import (
    AREA_BUCKET_LABELS,
)
from osm_polygon_description_tag.dataset.geography.atomic import (
    atomic_save_png as _atomic_save_png,
)

# Shared palette with the H3 density map so the dataset card feels like
# one document.
_BG_COLOR: Final[str] = "#ffffff"
_PANEL_COLOR: Final[str] = "#f7f7f4"
_TEXT_COLOR: Final[str] = "#222222"
_MUTED_COLOR: Final[str] = "#666666"
_BAR_COLOR: Final[str] = "#4a6fa5"
_BAR_EDGE: Final[str] = "#2f4a78"

# Layout constants.
_FIGSIZE: Final[tuple[float, float]] = (10.0, 6.5)
_DPI: Final[int] = 100

# Typography.
_TITLE_FONTSIZE: Final[int] = 14
_LABEL_FONTSIZE: Final[int] = 10
_TICK_FONTSIZE: Final[int] = 9
_CAPTION_FONTSIZE: Final[int] = 8

# Caption templates.
_TITLE: Final[str] = "Area distribution of description-tagged polygons"
_NO_DATA_CAPTION: Final[str] = (
    "Area distribution of description-tagged polygons. "
    "0 polygons across all area buckets (no data)."
)


@dataclass(frozen=True)
class AreaHistogramResult:
    """Outcome of :func:`render_area_histogram`.

    The PNG is written to ``output_path`` and the exact caption text
    rendered onto the figure is exposed so callers and tests can
    introspect it without parsing the rasterized image.
    """

    output_path: Path
    caption: str


def _format_count_tick(value: float, _position: int | None = None) -> str:
    """Format a histogram count tick as a human-readable integer label."""
    count = round(value)
    if count < 1_000:
        return str(count)
    if count < 1_000_000:
        thousands = count / 1_000.0
        return f"{thousands:.0f}k" if thousands.is_integer() else f"{thousands:.1f}k"
    millions = count / 1_000_000.0
    return f"{millions:.1f}M"


def _build_caption(counts: Mapping[str, int]) -> str:
    total = sum(int(value) for value in counts.values())
    if total == 0:
        return _NO_DATA_CAPTION
    occupied = sum(1 for value in counts.values() if int(value) > 0)
    return (
        f"Area distribution of description-tagged polygons. "
        f"{total:,} polygons bucketed into {occupied} of {len(counts)} "
        "logarithmic area bins (m²)."
    )


def _bar_labels(counts: Mapping[str, int]) -> tuple[list[str], list[int]]:
    """Return every bucket label with its count, including empty buckets."""
    return list(AREA_BUCKET_LABELS), [int(counts.get(label, 0)) for label in AREA_BUCKET_LABELS]


def render_area_histogram(
    counts: Mapping[str, int],
    output_path: Path,
) -> AreaHistogramResult:
    """Render the area histogram and atomically write it to ``output_path``.

    The ``counts`` argument maps each :data:`AREA_BUCKET_LABELS` entry
    to its polygon count. Identical inputs produce byte-identical PNGs;
    identical re-renders preserve the existing file's mtime.
    """
    labels, values = _bar_labels(counts)
    caption = _build_caption(counts)
    fig, ax = plt.subplots(figsize=_FIGSIZE, dpi=_DPI)
    try:
        positions = np.arange(len(labels))
        _style_area_figure(fig, ax)
        _draw_area_bars(ax, positions, values)
        _style_area_axes(ax, labels, positions)
        _annotate_area_bars(ax, positions, values)
        _style_area_grid(ax)
        _add_area_caption(fig, caption)
        _set_area_limits(ax, values)
        fig.tight_layout(rect=(0, 0.05, 1, 0.97))
        _atomic_save_png(fig, output_path)
    finally:
        plt.close(fig)

    return AreaHistogramResult(output_path=output_path, caption=caption)


def _style_area_figure(fig: Any, ax: Any) -> None:
    fig.set_facecolor(_BG_COLOR)
    ax.set_facecolor(_PANEL_COLOR)


def _draw_area_bars(ax: Any, positions: Any, values: Sequence[int]) -> None:
    # Keep empty buckets truly zero-width; substituting one would imply a
    # polygon that does not exist on the logarithmic axis.
    occupied_positions = [
        position for position, value in zip(positions, values, strict=True) if value > 0
    ]
    occupied_values = [value for value in values if value > 0]
    ax.barh(
        occupied_positions,
        occupied_values,
        color=_BAR_COLOR,
        edgecolor=_BAR_EDGE,
        linewidth=0.5,
        zorder=2,
    )


def _style_area_axes(ax: Any, labels: Sequence[str], positions: Any) -> None:
    ax.set_yticks(positions)
    ax.set_yticklabels(labels, fontsize=_TICK_FONTSIZE, color=_TEXT_COLOR)
    ax.invert_yaxis()
    ax.set_xscale("log")
    ax.set_xlabel("Polygons per bucket (log scale)", fontsize=_LABEL_FONTSIZE, color=_TEXT_COLOR)
    ax.tick_params(axis="x", colors=_MUTED_COLOR, labelsize=_TICK_FONTSIZE)
    ax.tick_params(axis="y", colors=_TEXT_COLOR)


def _annotate_area_bars(ax: Any, positions: Any, values: Sequence[int]) -> None:
    for position, count in zip(positions, values, strict=True):
        label_text = _format_count_tick(float(count))
        width = max(float(count), 1.0)
        ax.text(
            width * 1.08,
            position,
            label_text,
            va="center",
            ha="left",
            fontsize=_TICK_FONTSIZE,
            color=_TEXT_COLOR,
            zorder=4,
        )


def _style_area_grid(ax: Any) -> None:
    for spine in ("top", "right"):
        ax.spines[spine].set_visible(False)
    for spine in ("left", "bottom"):
        ax.spines[spine].set_color(_MUTED_COLOR)
    ax.grid(True, axis="x", color="#ffffff", linewidth=0.8, alpha=0.9, zorder=1)
    ax.set_axisbelow(True)
    ax.set_title(_TITLE, fontsize=_TITLE_FONTSIZE, color=_TEXT_COLOR, pad=12, loc="left")


def _add_area_caption(fig: Any, caption: str) -> None:
    fig.text(
        0.5,
        0.02,
        caption,
        ha="center",
        va="bottom",
        fontsize=_CAPTION_FONTSIZE,
        color=_MUTED_COLOR,
        wrap=True,
    )


def _set_area_limits(ax: Any, values: Sequence[int]) -> None:
    # Leave enough headroom on the right for the largest annotation.
    # pragma: no mutate start - empty fallback stays below the fixed lower bound of 10
    max_value = max(values) if values else 1
    # pragma: no mutate end
    ax.set_xlim(left=1.0, right=max(max_value * 4.0, 10.0))


__all__: Final[Sequence[str]] = (
    "AreaHistogramResult",
    "render_area_histogram",
)
