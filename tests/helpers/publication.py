"""Shared complete publication dataset builder for unit and acceptance tests."""

from pathlib import Path

from shapely.geometry import Polygon

from osm_polygon_description_tag.dataset.manifest import (
    Manifest,
    RunCounts,
    output_identity_for,
    source_identity_for,
    write_manifest,
)
from osm_polygon_description_tag.dataset.storage import write_geoparquet
from tests.conftest import make_record_dict


def build_publication_dataset(data_root: Path) -> None:
    (data_root / "data").mkdir(parents=True)
    (data_root / "manifests").mkdir(parents=True)
    (data_root / "README.md").write_text("# Card\n", encoding="utf-8")
    (data_root / "stats.json").write_text("{}\n", encoding="utf-8")
    (data_root / "assets").mkdir()
    (data_root / "assets" / "description_polygon_density.png").write_bytes(
        b"\x89PNG\r\n\x1a\n" + b"map" * 1024
    )
    (data_root / "assets" / "area_distribution.png").write_bytes(
        b"\x89PNG\r\n\x1a\n" + b"hist" * 1024
    )
    source_root = data_root.parent / "raw"
    source_root.mkdir(exist_ok=True)
    source = source_root / "a-latest.osm.pbf"
    source.write_bytes(b"a-latest-bytes")
    record = make_record_dict(
        Polygon([(0, 0), (0, 1), (1, 1), (1, 0)]),
        {"description": "x"},
        osm_id=1,
        source_pbf="a-latest.osm.pbf",
    )
    output = data_root / "data" / "a-latest.parquet"
    write_geoparquet(iter([record]), output, batch_size=10)
    manifest = Manifest(
        manifest_schema_version=2,
        schema_version=3,
        geoparquet_version="1.1.0",
        transform_algorithm_version=3,
        output_algorithm_revision="x" * 64,
        area_policy_sha256="0" * 64,
        source=source_identity_for(source),
        output=output_identity_for(output),
        osmium_version="osmium version 1.16.0",
        dependency_versions={"pyarrow": "20.0.0"},
        code_revision="abc123",
        started_at="2026-07-27T00:00:00+00:00",
        completed_at="2026-07-27T00:01:00+00:00",
        counts=RunCounts(emitted_features=1, included_rows=1, rejections={}),
    )
    write_manifest(manifest, data_root / "manifests" / "a-latest.manifest.json")
