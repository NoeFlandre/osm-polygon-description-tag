"""Synthetic benchmarks for the density-map renderer and CLI import time.

Sizes are small and seeded; scale with ``PERF_SCALE`` (default 1). Each
benchmark first checks its output so an optimisation cannot pass by changing
results. Run with ``just bench``.
"""

from __future__ import annotations

import os
import random
import subprocess
import sys

import pytest

pytest.importorskip("pytest_benchmark")

from osm_polygon_description_tag.dataset.geography.h3_policy import cell_rings
from osm_polygon_description_tag.dataset.geography.rendering import (
    render_density_map,
)

_SCALE = float(os.environ.get("PERF_SCALE", "1"))


def _synthetic_cells(count: int) -> dict[str, int]:
    import h3

    resolution = 3
    while h3.get_num_cells(resolution) < 2 * count:
        resolution += 1

    rng = random.Random(20260929)  # noqa: S311
    cells: dict[str, int] = {}
    while len(cells) < count:
        lat, lon = rng.uniform(-60, 70), rng.uniform(-170, 170)
        cells[h3.latlng_to_cell(lat, lon, resolution)] = rng.randint(1, 5000)
    return cells


def test_synthetic_cells_scales_resolution_before_the_grid_is_exhausted(monkeypatch) -> None:
    import h3

    count = h3.get_num_cells(3) // 2 + 1
    generated = iter(range(count))

    def fake_latlng_to_cell(lat: float, lon: float, resolution: int) -> str:
        del lat, lon
        if resolution == 3:
            return "res-3-single-cell"
        return f"res-{resolution}-{next(generated)}"

    monkeypatch.setattr(h3, "latlng_to_cell", fake_latlng_to_cell)

    cells = _synthetic_cells(count)

    assert len(cells) == count
    assert all(cell.startswith("res-4-") for cell in cells)


def test_render_density_map(benchmark, tmp_path) -> None:
    cells = _synthetic_cells(int(1000 * _SCALE))
    assert all(cell_rings(cell) for cell in list(cells)[:5])
    out = tmp_path / "map.png"

    result = benchmark.pedantic(
        render_density_map, args=(cells, out), kwargs={"land_features": []}, rounds=3
    )

    assert result.output_path == out
    assert out.read_bytes().startswith(b"\x89PNG")


def test_cli_import_time(benchmark) -> None:
    result = benchmark.pedantic(
        subprocess.run,
        args=([sys.executable, "-c", "import osm_polygon_description_tag.cli"],),
        kwargs={"check": True, "stdout": subprocess.DEVNULL},
        rounds=3,
    )

    assert result.returncode == 0
    assert benchmark.stats["mean"] < 5.0
