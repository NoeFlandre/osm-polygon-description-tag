"""Builders for finalized Parquet and manifest fixtures."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from pathlib import Path

from shapely.geometry import MultiPolygon, Polygon

from osm_polygon_description_tag.dataset.manifest import (
    Manifest,
    RunCounts,
    current_area_policy_sha256,
    current_output_algorithm_revision,
    output_identity_for,
    source_identity_for,
    write_manifest,
)
from osm_polygon_description_tag.dataset.storage import write_geoparquet
from tests.conftest import make_record_dict


def frozen_clock() -> str:
    """Return the stable timestamp used by deterministic artifact tests."""
    return "2026-07-27T00:00:00+00:00"


def write_finalized_dataset(
    data_root: Path,
    source_root: Path,
    shards: Mapping[str, Sequence[dict[str, object]]],
    *,
    rejections: Mapping[str, Mapping[str, int]] | None = None,
) -> None:
    """Write schema-valid Parquet/manifest pairs for isolated tests."""
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


def write_reporting_fixture(data_root: Path, source_root: Path) -> None:
    """Write the multi-geometry fixture used by reporting/media tests."""
    region_a = [
        make_record_dict(
            Polygon([(0, 0), (0, 1), (1, 1), (1, 0)]),
            {"description:en": "EN"},
            osm_type="way",
            osm_id=1,
            source_pbf="region-a.osm.pbf",
        ),
        make_record_dict(
            MultiPolygon([Polygon([(10, 10), (10, 11), (11, 11), (11, 10)])]),
            {"description:pt-BR": "PT"},
            osm_type="relation",
            osm_id=2,
            source_pbf="region-a.osm.pbf",
        ),
    ]
    region_b = [
        make_record_dict(
            Polygon([(0, 0), (0, 1), (1, 1), (1, 0)]),
            {"description:en": "EN"},
            osm_type="way",
            osm_id=3,
            source_pbf="region-b.osm.pbf",
        )
    ]
    write_finalized_dataset(
        data_root,
        source_root,
        {"region-a": region_a, "region-b": region_b},
        rejections={
            "region-a": {"no_nonempty_description": 2},
            "region-b": {"no_nonempty_description": 2},
        },
    )
