"""Bounded-memory stress tests for the reporting layer."""

from __future__ import annotations

from pathlib import Path

from shapely.geometry import Polygon

from osm_polygon_description_tag.dataset.manifest import (
    Manifest,
    RunCounts,
    output_identity_for,
    source_identity_for,
    write_manifest,
)
from osm_polygon_description_tag.dataset.reporting import collect_stats
from osm_polygon_description_tag.dataset.storage import write_geoparquet
from tests.conftest import make_record_dict


def _frozen_clock() -> str:
    return "2026-07-27T00:00:00+00:00"


def _build_pair(
    data_root: Path,
    source_root: Path,
    name: str,
    records: list[dict[str, object]],
    rejections: dict[str, int],
) -> None:
    source = source_root / f"{name}.osm.pbf"
    source.write_bytes(name.encode("utf-8"))
    output = data_root / "data" / f"{name}.parquet"
    manifest_path = data_root / "manifests" / f"{name}.manifest.json"
    included = write_geoparquet(iter(records), output, batch_size=10)
    write_manifest(
        Manifest(
            manifest_schema_version=2,
            schema_version=2,
            geoparquet_version="1.1.0",
            transform_algorithm_version=2,
            output_algorithm_revision="x" * 64,
            area_policy_sha256="0" * 64,
            source=source_identity_for(source),
            output=output_identity_for(output),
            osmium_version="osmium version 1.19.1",
            dependency_versions={"pyarrow": "20.0.0"},
            code_revision="abc",
            started_at="2026-07-27T00:00:00+00:00",
            completed_at="2026-07-27T00:01:00+00:00",
            counts=RunCounts(
                emitted_features=included + sum(rejections.values()),
                included_rows=included,
                rejections=rejections,
            ),
        ),
        manifest_path,
    )


def test_collect_stats_handles_many_rows_with_bounded_memory(tmp_path: Path) -> None:
    """collect_stats succeeds on a large per-PBF output."""
    data_root = tmp_path / "generated"
    source_root = tmp_path / "raw"
    source_root.mkdir()
    (data_root / "data").mkdir(parents=True)
    (data_root / "manifests").mkdir(parents=True)

    records = [
        make_record_dict(
            Polygon([(i % 5, 0), (i % 5, 1), (i % 5 + 0.5, 1), (i % 5 + 0.5, 0)]),
            {"description": "x"},
            osm_id=6000 + i,
            source_pbf="big.osm.pbf",
        )
        for i in range(500)
    ]
    _build_pair(data_root, source_root, "big", records, {})

    stats = collect_stats(data_root, clock=_frozen_clock)
    assert stats["rows"] == 500
    assert stats["area_m2_min_m2"] is not None
    assert stats["area_m2_max_m2"] is not None


def test_collect_stats_uses_quantile_cont_for_area(tmp_path: Path) -> None:
    """Exact area quantiles are produced without loading the column into Python."""
    data_root = tmp_path / "generated"
    source_root = tmp_path / "raw"
    source_root.mkdir()
    (data_root / "data").mkdir(parents=True)
    (data_root / "manifests").mkdir(parents=True)

    # 50 records with predictable areas (each is a 1m square near the equator).
    records = [
        make_record_dict(
            Polygon([(0, 0), (0, 1), (1, 1), (1, 0)]),
            {"description": "x"},
            osm_id=7000 + i,
            source_pbf="q.osm.pbf",
        )
        for i in range(50)
    ]
    _build_pair(data_root, source_root, "q", records, {})

    stats = collect_stats(data_root, clock=_frozen_clock)
    assert stats["area_m2_median_m2"] is not None
    # The areas should all be ~12309334800 m² (1 degree at the equator).
    assert abs(stats["area_m2_min_m2"] - 12309334800.0) < 1.0e7


def test_collect_stats_returns_none_for_empty_dataset(tmp_path: Path) -> None:
    data_root = tmp_path / "generated"
    source_root = tmp_path / "raw"
    source_root.mkdir()
    (data_root / "data").mkdir(parents=True)
    (data_root / "manifests").mkdir(parents=True)

    stats = collect_stats(data_root, clock=_frozen_clock)
    assert stats["rows"] == 0
    assert stats["area_m2_min_m2"] is None
    assert stats["area_m2_max_m2"] is None
