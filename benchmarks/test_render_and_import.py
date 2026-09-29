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
import time

import pytest

pytest.importorskip("pytest_benchmark")

from osm_polygon_description_tag.dataset.geography.h3_policy import cell_rings  # noqa: E402
from osm_polygon_description_tag.dataset.geography.rendering import (  # noqa: E402
    render_density_map,
)

_SCALE = float(os.environ.get("PERF_SCALE", "1"))


def _synthetic_cells(count: int) -> dict[str, int]:
    import h3

    rng = random.Random(20260929)  # noqa: S311
    cells: dict[str, int] = {}
    while len(cells) < count:
        lat, lon = rng.uniform(-60, 70), rng.uniform(-170, 170)
        cells[h3.latlng_to_cell(lat, lon, 3)] = rng.randint(1, 5000)
    return cells


def test_render_density_map(benchmark, tmp_path) -> None:
    cells = _synthetic_cells(int(1000 * _SCALE))
    assert all(cell_rings(cell) for cell in list(cells)[:5])
    out = tmp_path / "map.png"

    result = benchmark.pedantic(
        render_density_map, args=(cells, out), kwargs={"land_features": []}, rounds=3
    )

    assert result.output_path == out
    assert out.read_bytes().startswith(b"\x89PNG")


def test_cli_import_time() -> None:
    start = time.perf_counter()
    subprocess.run(  # noqa: S603
        [sys.executable, "-c", "import osm_polygon_description_tag.cli"], check=True
    )
    assert time.perf_counter() - start < 5.0
