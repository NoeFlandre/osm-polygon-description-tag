"""End-to-end population contract for overlapping regional artifacts."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from pathlib import Path

from shapely import to_wkb
from shapely.geometry import Polygon

from osm_polygon_description_tag.dataset.canonical_rows import select_canonical_row
from osm_polygon_description_tag.dataset.geography import (
    aggregate_area_histogram,
    aggregate_h3_density,
    assign_h3_cell,
)
from osm_polygon_description_tag.dataset.manifest import (
    Manifest,
    RunCounts,
    current_area_policy_sha256,
    current_output_algorithm_revision,
    output_identity_for,
    source_identity_for,
    write_manifest,
)
from osm_polygon_description_tag.dataset.stats import collect_stats
from osm_polygon_description_tag.dataset.storage import write_geoparquet
from osm_polygon_description_tag.dataset.transform import transform_record
from osm_polygon_description_tag.osm.extraction import ExportRecord


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


def _write_finalized_dataset(
    data_root: Path,
    source_root: Path,
    shards: Mapping[str, Sequence[dict[str, object]]],
    *,
    rejections: Mapping[str, Mapping[str, int]] | None = None,
) -> None:
    (data_root / "data").mkdir(parents=True, exist_ok=True)
    (data_root / "manifests").mkdir(parents=True, exist_ok=True)
    source_root.mkdir(parents=True, exist_ok=True)
    rejection_map = rejections or {}
    for name in sorted(shards):
        source_path = source_root / f"{name}.osm.pbf"
        source_path.write_bytes(name.encode("utf-8"))
        output_path = data_root / "data" / f"{name}.parquet"
        records = list(shards[name])
        included = write_geoparquet(iter(records), output_path, batch_size=10)
        counts = dict(rejection_map.get(name, {}))
        write_manifest(
            Manifest(
                manifest_schema_version=2,
                schema_version=3,
                geoparquet_version="1.1.0",
                transform_algorithm_version=3,
                area_policy_sha256=current_area_policy_sha256(),
                output_algorithm_revision=current_output_algorithm_revision(),
                source=source_identity_for(source_path),
                output=output_identity_for(output_path),
                osmium_version=None,
                dependency_versions={"pyarrow": "20.0.0"},
                code_revision=None,
                started_at="2026-01-01T00:00:00+00:00",
                completed_at="2026-01-01T00:00:01+00:00",
                counts=RunCounts(
                    emitted_features=included + sum(counts.values()),
                    included_rows=included,
                    rejections=counts,
                ),
            ),
            data_root / "manifests" / f"{name}.manifest.json",
        )


def _frozen_clock() -> str:
    return "2026-07-27T00:00:00+00:00"


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

    stats = collect_stats(data_root, clock=_frozen_clock)
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
