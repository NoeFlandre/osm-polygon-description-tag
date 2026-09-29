"""Micro-benchmarks for the per-feature transform.

Run with ``just bench``. The default test run does not collect this directory.
"""

from __future__ import annotations

import math

import pytest
from shapely import to_wkb
from shapely.geometry import Polygon

from osm_polygon_description_tag.dataset.transform import transform_record
from osm_polygon_description_tag.osm.extraction import ExportRecord

pytest.importorskip("pytest_benchmark")


def _record(vertices: int) -> ExportRecord:
    ring = [
        (
            10.0 + 0.01 * math.cos(2 * math.pi * i / vertices),
            50.0 + 0.01 * math.sin(2 * math.pi * i / vertices),
        )
        for i in range(vertices)
    ]
    wkb = to_wkb(Polygon(ring), include_srid=True, flavor="extended", byte_order=1)
    return ExportRecord(
        geometry_ewkb_hex=wkb.hex(),
        osm_type="way",
        osm_id=1,
        version=1,
        changeset=1,
        timestamp="2026-01-01T00:00:00Z",
        tags={"building": "yes", "name": "Nom", "description": "Une description"},
    )


@pytest.mark.parametrize("vertices", [8, 500, 5000])
def test_transform_record(benchmark, vertices: int) -> None:
    record = _record(vertices)

    row = benchmark(transform_record, record, "europe.osm.pbf")

    assert row["area_m2"] > 0
