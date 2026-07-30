"""RED tests for independent geodesic-area verification.

Each test computes its expected area independently using ``pyproj.Geod``
without calling the production ``geodesic_area_m2``. Tolerance is stated in
square metres or relative error.
"""

from __future__ import annotations

import math
from pathlib import Path

import pyarrow.parquet as pq
import pytest
from pyproj import Geod
from shapely import from_wkb
from shapely.geometry import MultiPolygon, Polygon

from osm_polygon_description_tag.dataset.storage import write_geoparquet
from tests.conftest import make_record_dict

GEOD = Geod(ellps="WGS84")


def _expected_area(geom: Polygon | MultiPolygon) -> float:
    """Return the geodesic area in square metres using only pyproj.Geod.

    For polygons with holes, the hole area is subtracted from the outer ring
    area (matching the production ``geodesic_area_m2`` semantics).
    """
    from shapely.ops import orient as shapely_orient

    oriented = shapely_orient(geom)
    area, _ = GEOD.geometry_area_perimeter(oriented)
    return abs(float(area))


def test_simple_polygon_area_matches_pyproj_within_tolerance(tmp_path: Path) -> None:
    """A simple 4-vertex polygon has a known geodesic area."""
    # ~111 m square at the equator: each side is 0.001 deg.
    poly = Polygon([(0, 0), (0, 0.001), (0.001, 0.001), (0.001, 0)])
    expected_area_m2 = _expected_area(poly)

    record = make_record_dict(poly, {"description": "x"}, osm_id=1, source_pbf="r.osm.pbf")
    target = tmp_path / "region.parquet"
    write_geoparquet(iter([record]), target, batch_size=1)

    table = pq.read_table(target)
    stored_area = float(table.column("area_m2")[0].as_py())
    # Tolerance: 0.1 m² for a ~12309 m² polygon (~0.001%).
    assert math.isclose(stored_area, expected_area_m2, rel_tol=1e-3, abs_tol=0.1)
    # Sanity: roughly 12 309 m² at the equator for a 0.001-degree square.
    assert 12000 < stored_area < 12600


def test_polygon_with_hole_subtracts_hole_area(tmp_path: Path) -> None:
    """Hole area must be subtracted from the outer ring's geodesic area."""
    outer = [(0, 0), (0, 0.002), (0.002, 0.002), (0.002, 0)]
    # Hole is half the width.
    hole = [(0.0005, 0.0005), (0.0005, 0.0015), (0.0015, 0.0015), (0.0015, 0.0005)]
    poly = Polygon(outer, holes=[hole])
    expected_area_m2 = _expected_area(poly)

    record = make_record_dict(poly, {"description": "x"}, osm_id=2, source_pbf="r.osm.pbf")
    target = tmp_path / "region.parquet"
    write_geoparquet(iter([record]), target, batch_size=1)

    table = pq.read_table(target)
    stored_area = float(table.column("area_m2")[0].as_py())
    assert math.isclose(stored_area, expected_area_m2, rel_tol=1e-3, abs_tol=0.5)


def test_multipolygon_area_is_sum_of_components(tmp_path: Path) -> None:
    """A MultiPolygon's stored area equals the sum of its components."""
    poly_a = Polygon([(0, 0), (0, 0.001), (0.001, 0.001), (0.001, 0)])
    poly_b = Polygon([(0.01, 0.01), (0.01, 0.011), (0.011, 0.011), (0.011, 0.01)])
    mp = MultiPolygon([poly_a, poly_b])
    expected_total = _expected_area(poly_a) + _expected_area(poly_b)

    record = make_record_dict(mp, {"description": "x"}, osm_id=3, source_pbf="r.osm.pbf")
    target = tmp_path / "region.parquet"
    write_geoparquet(iter([record]), target, batch_size=1)

    table = pq.read_table(target)
    stored_area = float(table.column("area_m2")[0].as_py())
    assert math.isclose(stored_area, expected_total, rel_tol=1e-3, abs_tol=0.5)


def test_stored_area_agrees_with_decoded_wkb(tmp_path: Path) -> None:
    """The stored area agrees with the area of the decoded WKB geometry."""
    poly = Polygon([(0, 0), (0, 0.001), (0.001, 0.001), (0.001, 0)])
    record = make_record_dict(poly, {"description": "x"}, osm_id=4, source_pbf="r.osm.pbf")
    target = tmp_path / "region.parquet"
    write_geoparquet(iter([record]), target, batch_size=1)

    table = pq.read_table(target)
    geom = from_wkb(table.column("geometry")[0].as_py())
    area, _ = GEOD.geometry_area_perimeter(geom)
    assert math.isclose(
        float(table.column("area_m2")[0].as_py()),
        abs(float(area)),
        rel_tol=1e-3,
        abs_tol=0.1,
    )


def test_stored_bounds_agrees_with_decoded_wkb(tmp_path: Path) -> None:
    """Stored bounding box agrees with the decoded WKB geometry bounds."""
    poly = Polygon([(0.1, 0.2), (0.1, 0.3), (0.2, 0.3), (0.2, 0.2)])
    record = make_record_dict(poly, {"description": "x"}, osm_id=5, source_pbf="r.osm.pbf")
    target = tmp_path / "region.parquet"
    write_geoparquet(iter([record]), target, batch_size=1)

    table = pq.read_table(target)
    geom = from_wkb(table.column("geometry")[0].as_py())
    minx, miny, maxx, maxy = geom.bounds
    assert math.isclose(table.column("bbox_min_x")[0].as_py(), minx, abs_tol=1e-9)
    assert math.isclose(table.column("bbox_min_y")[0].as_py(), miny, abs_tol=1e-9)
    assert math.isclose(table.column("bbox_max_x")[0].as_py(), maxx, abs_tol=1e-9)
    assert math.isclose(table.column("bbox_max_y")[0].as_py(), maxy, abs_tol=1e-9)


def test_area_for_distant_polygon_uses_geodesic_not_flat(tmp_path: Path) -> None:
    """At 60 degrees latitude, the geodesic area differs from the planar area.

    The same 0.001-degree polygon at the equator and at 60 deg N must produce
    different stored areas because WGS84 is not a flat plane.
    """
    equator = Polygon([(0, 0), (0, 0.001), (0.001, 0.001), (0.001, 0)])
    northern = Polygon([(0, 60), (0, 60.001), (0.001, 60.001), (0.001, 60)])

    records = [
        make_record_dict(equator, {"description": "x"}, osm_id=10, source_pbf="r.osm.pbf"),
        make_record_dict(northern, {"description": "x"}, osm_id=11, source_pbf="r.osm.pbf"),
    ]
    target = tmp_path / "region.parquet"
    write_geoparquet(iter(records), target, batch_size=2)

    table = pq.read_table(target)
    area_at_equator = _expected_area(equator)
    area_at_60 = _expected_area(northern)
    stored = sorted(float(v) for v in table.column("area_m2").to_pylist())
    assert stored[0] == pytest.approx(area_at_60, rel=1e-3, abs=0.5)
    assert stored[1] == pytest.approx(area_at_equator, rel=1e-3, abs=0.5)
    # Areas at different latitudes must be measurably different.
    assert abs(area_at_equator - area_at_60) > 1.0
