"""End-to-end population contract for overlapping regional artifacts."""

from __future__ import annotations

from pathlib import Path

from shapely import to_wkb
from shapely.geometry import Polygon

from osm_polygon_description_tag.dataset.canonical_rows import select_canonical_row
from osm_polygon_description_tag.dataset.geography import (
    aggregate_area_histogram,
    aggregate_h3_density,
    assign_h3_cell,
)
from osm_polygon_description_tag.dataset.stats import collect_stats
from osm_polygon_description_tag.dataset.transform import transform_record
from osm_polygon_description_tag.osm.extraction import ExportRecord
from tests.helpers.dataset import write_finalized_dataset as _write_finalized_dataset


def _make_record(
    geometry: Polygon,
    tags: dict[str, str],
    *,
    osm_id: int,
    version: int,
    source_pbf: str,
) -> dict[str, object]:
    record = ExportRecord(
        geometry_ewkb_hex=to_wkb(
            geometry, include_srid=True, flavor="extended", byte_order=1
        ).hex(),
        osm_type="way",
        osm_id=osm_id,
        version=version,
        changeset=10,
        timestamp="2026-01-01T00:00:00Z",
        tags=tags,
    )
    return transform_record(record, source_pbf)


def test_stats_map_and_area_use_the_same_global_text_population(tmp_path: Path) -> None:
    """Regional overlap rows count once, using the deterministic winning geometry."""
    old_geometry = Polygon([(0, 0), (0, 1), (1, 1), (1, 0)])
    winning_geometry = Polygon([(10, 10), (10, 11), (11, 11), (11, 10)])
    localized_geometry = Polygon([(20, 20), (20, 21), (21, 21), (21, 20)])
    older = _make_record(
        old_geometry,
        {"description": "older regional copy"},
        osm_id=101,
        version=1,
        source_pbf="region-a.osm.pbf",
    )
    winner = _make_record(
        winning_geometry,
        {"description": "winning regional copy"},
        osm_id=101,
        version=2,
        source_pbf="region-b.osm.pbf",
    )
    localized = _make_record(
        localized_geometry,
        {"description:en": "localized only"},
        osm_id=202,
        version=1,
        source_pbf="region-a.osm.pbf",
    )
    data_root = tmp_path / "generated"
    _write_finalized_dataset(
        data_root,
        tmp_path / "raw",
        {"region-a": [older, localized], "region-b": [winner]},
        rejections={"region-a": {"no_nonempty_description": 3}},
    )

    stats = collect_stats(data_root)
    h3_counts = aggregate_h3_density(data_root)
    area_counts = aggregate_area_histogram(data_root)

    canonical = select_canonical_row((older, winner), require_successful_text=True)
    expected_cells = {
        assign_h3_cell(10.5, 10.5): 1,
        assign_h3_cell(20.5, 20.5): 1,
    }

    assert stats["regional_rows"] == 3
    assert stats["globally_unique_polygons"] == 2
    assert stats["unique_polygons_with_successful_nonempty_text"] == 2
    assert stats["regional_overlap_duplicate_rows"] == 1
    assert stats["text_rejection_counts"]["no_nonempty_description"] == 3
    assert stats["area_m2_count"] == 2
    assert stats["area_m2_total_m2"] == canonical["area_m2"] + localized["area_m2"]
    assert h3_counts == expected_cells
    assert sum(h3_counts.values()) == stats["unique_polygons_with_successful_nonempty_text"]
    assert sum(area_counts.values()) == stats["area_m2_count"]
