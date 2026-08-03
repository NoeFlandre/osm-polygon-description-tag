"""Legacy Arrow-map migration tests."""

import json
from pathlib import Path

import pyarrow as pa
import pyarrow.parquet as pq
from shapely import to_wkb
from shapely.geometry import Polygon

from osm_polygon_description_tag.dataset.manifest import (
    MANIFEST_SCHEMA_VERSION,
    Manifest,
    RunCounts,
    SourceIdentity,
    current_area_policy_sha256,
    current_output_algorithm_revision,
    output_identity_for,
    read_manifest,
    write_manifest,
)
from osm_polygon_description_tag.dataset.migration import migrate_dataset_schema
from osm_polygon_description_tag.dataset.schema import SCHEMA, SCHEMA_VERSION


def test_migrate_legacy_maps_updates_parquet_and_manifest(tmp_path: Path) -> None:
    data_root = tmp_path
    data_dir = data_root / "data"
    manifests_dir = data_root / "manifests"
    data_dir.mkdir()
    manifests_dir.mkdir()
    parquet = data_dir / "region.parquet"

    fields = [
        pa.field(field.name, pa.map_(pa.string(), pa.string()))
        if field.name in {"localized_names", "localized_descriptions", "tags"}
        else field
        for field in SCHEMA
    ]
    legacy_schema = pa.schema(fields)
    row = {
        "source_pbf": "region.osm.pbf",
        "osm_type": "way",
        "osm_id": 1,
        "osm_url": "https://www.openstreetmap.org/way/1",
        "version": 1,
        "changeset": 1,
        "timestamp": None,
        "name": "Example",
        "localized_names": {"en": "Example"},
        "description": "A polygon",
        "localized_descriptions": {"fr": "Un polygone"},
        "tags": {"description": "A polygon", "name": "Example"},
        "geometry_type": "Polygon",
        "area_m2": 1.0,
        "bbox_min_x": 0.0,
        "bbox_min_y": 0.0,
        "bbox_max_x": 1.0,
        "bbox_max_y": 1.0,
        "geometry": to_wkb(Polygon([(0, 0), (0, 1), (1, 1), (1, 0)])),
    }
    table = pa.Table.from_pylist([row], schema=legacy_schema)
    metadata = legacy_schema.with_metadata(
        {
            b"geo": json.dumps(
                {
                    "version": "1.1.0",
                    "primary_column": "geometry",
                    "columns": {
                        "geometry": {
                            "encoding": "WKB",
                            "geometry_types": ["Polygon"],
                            "bbox": [0.0, 0.0, 1.0, 1.0],
                        }
                    },
                }
            ).encode()
        }
    )
    pq.write_table(table.cast(metadata), parquet, compression="zstd")

    manifest = Manifest(
        manifest_schema_version=MANIFEST_SCHEMA_VERSION,
        schema_version=2,
        geoparquet_version="1.1.0",
        transform_algorithm_version=2,
        area_policy_sha256=current_area_policy_sha256(),
        output_algorithm_revision=current_output_algorithm_revision(),
        source=SourceIdentity("region.osm.pbf", 1, 1, "a" * 64),
        output=output_identity_for(parquet),
        osmium_version=None,
        dependency_versions={},
        code_revision=None,
        started_at="2026-01-01T00:00:00+00:00",
        completed_at="2026-01-01T00:00:00+00:00",
        counts=RunCounts(1, 1, {}),
    )
    manifest_path = manifests_dir / "region.manifest.json"
    write_manifest(manifest, manifest_path)

    assert migrate_dataset_schema(data_root) == 1
    migrated_schema = pq.read_schema(parquet)
    assert migrated_schema.names == SCHEMA.names
    assert migrated_schema.field("tags").type == SCHEMA.field("tags").type
    assert pq.read_table(parquet).column("tags").to_pylist() == [
        [{"key": "description", "value": "A polygon"}, {"key": "name", "value": "Example"}]
    ]
    migrated_manifest = read_manifest(manifest_path)
    assert migrated_manifest.schema_version == SCHEMA_VERSION
    assert migrated_manifest.transform_algorithm_version == 3
    assert migrated_manifest.output == output_identity_for(parquet)
    assert migrate_dataset_schema(data_root) == 0
