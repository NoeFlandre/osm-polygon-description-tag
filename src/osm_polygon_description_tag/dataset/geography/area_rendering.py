"""Deterministic PNG rendering of the area distribution histogram.

The renderer is a pure function of the bucket counts and the chosen
output path. It never downloads anything and produces a self-contained
horizontal bar chart with stable visual constants for byte-for-byte
determinism.
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

import matplotlib.pyplot as plt
import numpy as np

from osm_polygon_description_tag.dataset.geography.area_histogram import (
    AREA_BUCKET_LABELS,
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

# PNG metadata.
_METADATA_SOFTWARE: Final[str] = "osm-polygon-description-tag"

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
    """Return aligned label/value lists, dropping empty leading buckets for legibility."""
    labels: list[str] = []
    values: list[int] = []
    for label in AREA_BUCKET_LABELS:
        count = int(counts.get(label, 0))
        labels.append(label)
        values.append(count)
    return labels, values


def _atomic_save_png(fig: Any, output_path: Path) -> None:
    """Save ``fig`` to ``output_path`` via a temporary file then atomic rename."""
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
    total = sum(values)

    fig, ax = plt.subplots(figsize=_FIGSIZE, dpi=_DPI)
    try:
        fig.set_facecolor(_BG_COLOR)
        ax.set_facecolor(_PANEL_COLOR)

        positions = np.arange(len(labels))
        # Some buckets may be empty (most prominently the smallest).
        # Plotting them as zero-width bars keeps the y-axis uniform and
        # the labels legible even when nothing falls in a given range.
        bar_values = [max(v, 1) if total > 0 else 1 for v in values]
        bars = ax.barh(
            positions,
            bar_values,
            color=_BAR_COLOR,
            edgecolor=_BAR_EDGE,
            linewidth=0.5,
            zorder=2,
        )

        ax.set_yticks(positions)
        ax.set_yticklabels(labels, fontsize=_TICK_FONTSIZE, color=_TEXT_COLOR)
        ax.invert_yaxis()  # smallest area at the top, largest at the bottom

        ax.set_xscale("log")
        ax.set_xlabel(
            "Polygons per bucket (log scale)", fontsize=_LABEL_FONTSIZE, color=_TEXT_COLOR
        )
        ax.tick_params(axis="x", colors=_MUTED_COLOR, labelsize=_TICK_FONTSIZE)
        ax.tick_params(axis="y", colors=_TEXT_COLOR)

        # Annotate each bar with its exact integer count so the chart is
        # readable at a glance even when the y-axis is log-scaled.
        for bar, count in zip(bars, values, strict=False):
            label_text = _format_count_tick(float(count))
            width = max(bar.get_width(), 1.0)
            ax.text(
                width * 1.08,
                bar.get_y() + bar.get_height() / 2.0,
                label_text,
                va="center",
                ha="left",
                fontsize=_TICK_FONTSIZE,
                color=_TEXT_COLOR,
                zorder=4,
            )

        for spine in ("top", "right"):
            ax.spines[spine].set_visible(False)
        for spine in ("left", "bottom"):
            ax.spines[spine].set_color(_MUTED_COLOR)

        ax.grid(True, axis="x", color="#ffffff", linewidth=0.8, alpha=0.9, zorder=1)
        ax.set_axisbelow(True)

        ax.set_title(_TITLE, fontsize=_TITLE_FONTSIZE, color=_TEXT_COLOR, pad=12, loc="left")
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

        # Leave enough headroom on the right for the largest annotation.
        max_value = max(values) if values else 1
        ax.set_xlim(right=max(max_value * 4.0, 10.0))

        fig.tight_layout(rect=(0, 0.05, 1, 0.97))
        _atomic_save_png(fig, output_path)
    finally:
        plt.close(fig)

    return AreaHistogramResult(output_path=output_path, caption=caption)


def atomic_save_png_for_testing(fig: Any, output_path: Path) -> None:
    """Test-only re-export of :func:`_atomic_save_png` for failure-path coverage."""
    _atomic_save_png(fig, output_path)


__all__: Final[Sequence[str]] = (
    "AreaHistogramResult",
    "render_area_histogram",
)
