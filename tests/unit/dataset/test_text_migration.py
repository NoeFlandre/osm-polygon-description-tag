"""Legacy untrimmed-description migration tests.

These cover the artifacts built before the successful-text contract existed:
the stored values are valid text but carry leading or trailing whitespace,
which the final-artifact contract rejects.
"""

import json
import re
from pathlib import Path

import pyarrow as pa
import pyarrow.parquet as pq
import pytest
from shapely import to_wkb
from shapely.geometry import Polygon

from osm_polygon_description_tag.dataset import text_migration
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
from osm_polygon_description_tag.dataset.schema import SCHEMA, SCHEMA_VERSION
from osm_polygon_description_tag.dataset.storage import validate_geoparquet
from osm_polygon_description_tag.dataset.text_migration import (
    TextMigrationError,
    migrate_dataset_text,
)

_GEO_METADATA = {
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


def _row(
    osm_id: int,
    description: object,
    localized: list[dict[str, object]] | None = None,
) -> dict[str, object]:
    return {
        "source_pbf": "region.osm.pbf",
        "osm_type": "way",
        "osm_id": osm_id,
        "osm_url": f"https://www.openstreetmap.org/way/{osm_id}",
        "version": 1,
        "changeset": 1,
        "timestamp": None,
        "name": "Example",
        "localized_names": [{"key": "en", "value": "Example"}],
        "description": description,
        "localized_descriptions": localized if localized is not None else [],
        "tags": [{"key": "name", "value": "Example"}],
        "geometry_type": "Polygon",
        "area_m2": 1.0,
        "bbox_min_x": 0.0,
        "bbox_min_y": 0.0,
        "bbox_max_x": 1.0,
        "bbox_max_y": 1.0,
        "geometry": to_wkb(Polygon([(0, 0), (0, 1), (1, 1), (1, 0)])),
    }


def _write_parquet(path: Path, rows: list[dict[str, object]]) -> None:
    table = pa.Table.from_pylist(rows, schema=SCHEMA)
    pq.write_table(table.cast(SCHEMA.with_metadata(_GEO_METADATA)), path, compression="zstd")


def _write_manifest(path: Path, parquet: Path, *, included_rows: int) -> None:
    write_manifest(
        Manifest(
            manifest_schema_version=MANIFEST_SCHEMA_VERSION,
            schema_version=SCHEMA_VERSION,
            geoparquet_version="1.1.0",
            transform_algorithm_version=3,
            area_policy_sha256=current_area_policy_sha256(),
            output_algorithm_revision=current_output_algorithm_revision(),
            source=SourceIdentity("region.osm.pbf", 1, 1, "a" * 64),
            output=output_identity_for(parquet),
            osmium_version=None,
            dependency_versions={},
            code_revision=None,
            started_at="2026-01-01T00:00:00+00:00",
            completed_at="2026-01-01T00:00:00+00:00",
            counts=RunCounts(included_rows, included_rows, {}),
        ),
        path,
    )


def _prepare(tmp_path: Path, rows: list[dict[str, object]]) -> tuple[Path, Path, Path]:
    tmp_path.mkdir(parents=True, exist_ok=True)
    data_dir = tmp_path / "data"
    manifests_dir = tmp_path / "manifests"
    data_dir.mkdir()
    manifests_dir.mkdir()
    parquet = data_dir / "region.parquet"
    manifest_path = manifests_dir / "region.manifest.json"
    _write_parquet(parquet, rows)
    _write_manifest(manifest_path, parquet, included_rows=len(rows))
    return tmp_path, parquet, manifest_path


def test_trims_base_description_and_leaves_the_row_counted(tmp_path: Path) -> None:
    data_root, parquet, manifest_path = _prepare(tmp_path, [_row(1, "Sahati Clock Tower\n")])

    assert migrate_dataset_text(data_root) == 1

    assert pq.read_table(parquet).column("description").to_pylist() == ["Sahati Clock Tower"]
    assert validate_geoparquet(parquet) == 1
    assert read_manifest(manifest_path).counts.included_rows == 1


def test_trims_localized_description_values(tmp_path: Path) -> None:
    data_root, parquet, _ = _prepare(
        tmp_path,
        [_row(1, None, [{"key": "fr", "value": "  Un polygone  "}])],
    )

    assert migrate_dataset_text(data_root) == 1

    assert pq.read_table(parquet).column("localized_descriptions").to_pylist() == [
        [{"key": "fr", "value": "Un polygone"}]
    ]
    assert validate_geoparquet(parquet) == 1


def test_drops_a_row_whose_text_is_only_whitespace(tmp_path: Path) -> None:
    data_root, parquet, manifest_path = _prepare(
        tmp_path,
        [_row(1, "   "), _row(2, "Kept description")],
    )

    assert migrate_dataset_text(data_root) == 1

    table = pq.read_table(parquet)
    assert table.column("osm_id").to_pylist() == [2]
    assert table.column("description").to_pylist() == ["Kept description"]
    assert validate_geoparquet(parquet) == 1
    counts = read_manifest(manifest_path).counts
    assert counts.included_rows == 1
    assert counts.rejections == {"no_nonempty_description": 1}


def test_keeps_a_row_whose_localized_text_survives_a_blank_base(tmp_path: Path) -> None:
    data_root, parquet, _ = _prepare(
        tmp_path,
        [_row(1, "  ", [{"key": "fr", "value": " Un polygone "}])],
    )

    assert migrate_dataset_text(data_root) == 1

    table = pq.read_table(parquet)
    assert table.column("description").to_pylist() == [None]
    assert table.column("localized_descriptions").to_pylist() == [
        [{"key": "fr", "value": "Un polygone"}]
    ]
    assert validate_geoparquet(parquet) == 1


def test_drops_a_blank_localized_entry_rather_than_storing_it(tmp_path: Path) -> None:
    data_root, parquet, _ = _prepare(
        tmp_path,
        [
            _row(
                1, "Kept", [{"key": "fr", "value": "   "}, {"key": "de", "value": " Beschreibung "}]
            )
        ],
    )

    assert migrate_dataset_text(data_root) == 1

    assert pq.read_table(parquet).column("localized_descriptions").to_pylist() == [
        [{"key": "de", "value": "Beschreibung"}]
    ]
    assert validate_geoparquet(parquet) == 1


def test_a_second_run_is_a_no_op(tmp_path: Path) -> None:
    data_root, parquet, manifest_path = _prepare(tmp_path, [_row(1, "Sahati Clock Tower\n")])

    assert migrate_dataset_text(data_root) == 1
    first_identity = output_identity_for(parquet)
    first_manifest = manifest_path.read_bytes()

    assert migrate_dataset_text(data_root) == 0
    assert output_identity_for(parquet) == first_identity
    assert manifest_path.read_bytes() == first_manifest


def test_an_already_clean_artifact_is_untouched(tmp_path: Path) -> None:
    data_root, parquet, manifest_path = _prepare(tmp_path, [_row(1, "Already clean")])
    before = parquet.read_bytes()
    before_manifest = manifest_path.read_bytes()

    assert migrate_dataset_text(data_root) == 0
    assert parquet.read_bytes() == before
    assert manifest_path.read_bytes() == before_manifest


def test_the_manifest_output_identity_matches_the_rewritten_parquet(tmp_path: Path) -> None:
    data_root, parquet, manifest_path = _prepare(tmp_path, [_row(1, "Trailing space ")])

    assert migrate_dataset_text(data_root) == 1
    assert read_manifest(manifest_path).output == output_identity_for(parquet)


def test_missing_directories_are_reported(tmp_path: Path) -> None:
    with pytest.raises(TextMigrationError, match=re.escape(str(tmp_path))):
        migrate_dataset_text(tmp_path)


def test_a_row_that_cannot_be_repaired_is_reported(tmp_path: Path) -> None:
    """A defect the trim cannot fix must abort the artifact, not be promoted."""
    data_root, parquet, _ = _prepare(tmp_path, [_row(1, "Trailing space ")])
    table = pq.read_table(parquet)
    broken = table.set_column(
        table.schema.get_field_index("area_m2"),
        "area_m2",
        pa.array([-1.0], pa.float64()),
    )
    pq.write_table(broken.cast(SCHEMA.with_metadata(_GEO_METADATA)), parquet, compression="zstd")

    with pytest.raises(TextMigrationError, match=re.escape(str(parquet))):
        migrate_dataset_text(data_root)


def test_the_temporary_file_is_removed_when_the_rewrite_fails(tmp_path: Path) -> None:
    data_root, parquet, _ = _prepare(tmp_path, [_row(1, "Trailing space ")])

    with pytest.raises(TextMigrationError):
        text_migration._migrate_parquet_text(parquet, _failing_writer)

    assert list(parquet.parent.glob(".*.tmp")) == []


def _failing_writer(*_args: object, **_kwargs: object) -> None:
    raise OSError("disk full")


def test_concurrent_and_sequential_runs_agree(tmp_path: Path) -> None:
    """Worker count is an execution detail, never a change in the result."""
    rows = [_row(index, f"Description {index} ") for index in range(1, 5)]
    sequential_root, sequential_parquet, _ = _prepare(tmp_path / "sequential", rows)
    concurrent_root, concurrent_parquet, _ = _prepare(tmp_path / "concurrent", rows)

    assert migrate_dataset_text(sequential_root) == 1
    assert migrate_dataset_text(concurrent_root, max_workers=4) == 1

    assert sequential_parquet.read_bytes() == concurrent_parquet.read_bytes()
    assert pq.read_table(concurrent_parquet).column("description").to_pylist() == [
        f"Description {index}" for index in range(1, 5)
    ]
