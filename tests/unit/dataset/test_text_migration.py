"""Legacy untrimmed-description migration tests.

These cover the artifacts built before the successful-text contract existed:
the stored values are valid text but carry leading or trailing whitespace,
which the final-artifact contract rejects.
"""

import json
import re
from dataclasses import replace
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


def test_the_temporary_file_is_removed_when_the_rewrite_fails(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    data_root, parquet, _ = _prepare(tmp_path, [_row(1, "Trailing space ")])
    monkeypatch.setattr(text_migration, "_rewrite_parquet_text", _failing_writer)

    with pytest.raises(TextMigrationError):
        text_migration._migrate_parquet_text(parquet)

    assert list(parquet.parent.glob(".*.tmp")) == []


def _failing_writer(*_args: object, **_kwargs: object) -> int:
    raise OSError("disk full")


def test_a_non_sequence_localized_container_yields_no_entries() -> None:
    assert text_migration._canonical_localized(None) == []
    assert text_migration._canonical_localized("not a sequence") == []


def test_a_malformed_localized_entry_is_dropped() -> None:
    entries = [
        "not a mapping",
        {"key": 7, "value": "numeric key"},
        {"key": "fr", "value": None},
        {"key": "de", "value": " Beschreibung "},
    ]

    assert text_migration._canonical_localized(entries) == [{"key": "de", "value": "Beschreibung"}]


def test_canonical_row_rewrites_base_and_localized_text_fields() -> None:
    row = _row(
        1,
        "  Base description ",
        [{"key": "fr", "value": "  Description française  "}],
    )

    assert text_migration._canonical_row(row) == {
        **row,
        "description": "Base description",
        "localized_descriptions": [{"key": "fr", "value": "Description française"}],
    }


def test_canonical_row_keeps_localized_text_when_base_text_is_blank() -> None:
    row = _row(1, "   ", [{"key": "fr", "value": " Un polygone "}])

    result = text_migration._canonical_row(row)

    assert result is not None
    assert result["description"] is None
    assert result["localized_descriptions"] == [{"key": "fr", "value": "Un polygone"}]


def test_row_changed_treats_an_unchanged_nonempty_localized_value_as_unchanged() -> None:
    localized = [{"key": "fr", "value": "Un polygone"}]
    before = {"description": None, "localized_descriptions": localized}
    after = {"description": None, "localized_descriptions": localized}

    assert text_migration._row_changed(before, after) is False


def test_text_columns_rejects_mismatched_column_lengths() -> None:
    with pytest.raises(ValueError):
        text_migration._text_columns(
            pa.table(
                {
                    "description": ["one"],
                    "localized_descriptions": [[], []],
                }
            )
        )


def test_retained_mask_rejects_mismatched_column_lengths() -> None:
    with pytest.raises(ValueError):
        text_migration._retained_mask(["one"], [[], []])


def test_canonical_table_preserves_schema_when_all_rows_are_dropped() -> None:
    table = pa.Table.from_pylist([_row(1, "   "), _row(2, "\t")], schema=SCHEMA)

    repaired, dropped = text_migration._canonical_table(table)

    assert dropped == 2
    assert repaired.schema == SCHEMA
    assert repaired.num_rows == 0


def test_canonical_table_counts_only_rows_removed_by_the_mask() -> None:
    table = pa.Table.from_pylist([_row(1, "   "), _row(2, "kept")], schema=SCHEMA)

    repaired, dropped = text_migration._canonical_table(table)

    assert dropped == 1
    assert repaired.column("osm_id").to_pylist() == [2]


def test_geo_metadata_keeps_inherited_values_and_writes_the_geo_key() -> None:
    table = pa.Table.from_pylist([_row(1, "kept")], schema=SCHEMA)

    schema = text_migration._geo_metadata_for(table, {b"custom": b"value"})

    assert schema.metadata is not None
    assert schema.metadata[b"custom"] == b"value"
    assert b"geo" in schema.metadata
    assert json.loads(schema.metadata[b"geo"])["columns"]["geometry"]["bbox"] == [
        0.0,
        0.0,
        1.0,
        1.0,
    ]


def test_geo_metadata_omits_the_bbox_for_an_empty_table() -> None:
    """An empty file has no extent, so GeoParquet omits ``bbox`` entirely."""
    table = pa.Table.from_pylist([], schema=SCHEMA)

    schema = text_migration._geo_metadata_for(table, {})

    assert schema.metadata is not None
    column = json.loads(schema.metadata[b"geo"])["columns"]["geometry"]
    assert "bbox" not in column


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


def _row_with(
    osm_id: int,
    description: object,
    *,
    geometry_type: str = "Polygon",
    bbox: tuple[float, float, float, float] = (0.0, 0.0, 1.0, 1.0),
    tags: list[dict[str, object]] | None = None,
) -> dict[str, object]:
    row = _row(osm_id, description)
    min_x, min_y, max_x, max_y = bbox
    row["geometry_type"] = geometry_type
    row["bbox_min_x"] = min_x
    row["bbox_min_y"] = min_y
    row["bbox_max_x"] = max_x
    row["bbox_max_y"] = max_y
    row["geometry"] = to_wkb(
        Polygon([(min_x, min_y), (min_x, max_y), (max_x, max_y), (max_x, min_y)])
    )
    if tags is not None:
        row["tags"] = tags
    return row


def test_dropping_the_extent_holder_rebuilds_the_geo_metadata(tmp_path: Path) -> None:
    """A dropped row must not leave the geo block describing rows that are gone."""
    data_root, parquet, _ = _prepare(
        tmp_path,
        [
            _row_with(1, "   ", bbox=(-50.0, -40.0, -49.0, -39.0)),
            _row_with(2, "Kept description", bbox=(0.0, 0.0, 1.0, 1.0)),
        ],
    )

    assert migrate_dataset_text(data_root) == 1

    table = pq.read_table(parquet)
    assert table.column("osm_id").to_pylist() == [2]
    assert validate_geoparquet(parquet) == 1
    geo = json.loads(pq.read_schema(parquet).metadata[b"geo"])
    assert geo["columns"]["geometry"]["bbox"] == [0.0, 0.0, 1.0, 1.0]


def test_dropping_the_only_row_of_a_geometry_type_rebuilds_the_type_list(
    tmp_path: Path,
) -> None:
    data_root, parquet, _ = _prepare(
        tmp_path,
        [
            _row_with(1, "  ", geometry_type="MultiPolygon"),
            _row_with(2, "Kept description", geometry_type="Polygon"),
        ],
    )

    assert migrate_dataset_text(data_root) == 1

    assert validate_geoparquet(parquet) == 1
    geo = json.loads(pq.read_schema(parquet).metadata[b"geo"])
    assert geo["columns"]["geometry"]["geometry_types"] == ["Polygon"]


def test_non_text_columns_are_carried_through_untouched(tmp_path: Path) -> None:
    """The repair may only touch description text, never tags or names."""
    tags = [
        {"key": "zebra", "value": "last"},
        {"key": "alpha", "value": "first"},
        {"key": "nulled", "value": None},
    ]
    data_root, parquet, _ = _prepare(tmp_path, [_row_with(1, "Trailing space ", tags=tags)])

    assert migrate_dataset_text(data_root) == 1

    table = pq.read_table(parquet)
    assert table.column("description").to_pylist() == ["Trailing space"]
    assert table.column("tags").to_pylist() == [tags]
    assert table.column("localized_names").to_pylist() == [[{"key": "en", "value": "Example"}]]


def test_a_stale_manifest_from_an_interrupted_run_is_healed(tmp_path: Path) -> None:
    """A crash between promotion and the manifest write must be recoverable."""
    data_root, parquet, manifest_path = _prepare(tmp_path, [_row(1, "Sahati Clock Tower\n")])
    assert migrate_dataset_text(data_root) == 1

    healthy = read_manifest(manifest_path)
    write_manifest(
        replace(healthy, output=replace(healthy.output, sha256="c" * 64)),
        manifest_path,
    )

    assert migrate_dataset_text(data_root) == 1
    assert read_manifest(manifest_path).output == output_identity_for(parquet)
    assert migrate_dataset_text(data_root) == 0


def test_the_migration_probe_reads_only_the_text_columns_in_bounded_batches(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The probe must not pull geometry just to inspect description text.

    ``description`` and ``localized_descriptions`` are small; ``geometry`` is
    the WKB blob and dominates the file. Reading every column, or reading the
    file in one unbounded batch, turns a cheap text check into a full scan of
    the dataset, so both arguments are part of the contract rather than hints.
    """
    _, parquet, _ = _prepare(tmp_path / "probe", [_row(1, "Already canonical")])
    seen: list[dict[str, object]] = []
    original = pq.ParquetFile.iter_batches

    def _spy(self: pq.ParquetFile, **kwargs: object):
        seen.append(dict(kwargs))
        return original(self, **kwargs)

    monkeypatch.setattr(pq.ParquetFile, "iter_batches", _spy)

    text_migration._requires_text_migration(parquet)

    assert seen == [
        {
            "batch_size": text_migration._BATCH_SIZE,
            "columns": ["description", "localized_descriptions"],
        }
    ]


def test_the_codec_is_pinned_to_zstd_by_one_named_constant() -> None:
    """The dataset contract fixes the codec, so its spelling is pinned here."""
    from osm_polygon_description_tag.dataset import storage

    assert storage.GEOPARQUET_COMPRESSION == "zstd"


def test_a_migrated_artifact_keeps_the_pinned_codec_and_dictionary_columns(
    tmp_path: Path,
) -> None:
    """Rewriting text must not quietly change how the artifact is stored.

    Dropping the codec leaves the file uncompressed and dropping the dictionary
    columns inflates the low-cardinality ones, so both are asserted from the
    written file rather than from the call.
    """
    from osm_polygon_description_tag.dataset.storage import (
        _DICTIONARY_COLUMNS,
        GEOPARQUET_COMPRESSION,
    )

    data_root, parquet, _ = _prepare(tmp_path, [_row(1, "  Needs trimming  "), _row(2, "Kept")])

    assert migrate_dataset_text(data_root) == 1

    metadata = pq.ParquetFile(parquet).metadata
    group = metadata.row_group(0)
    names = [metadata.schema.column(i).name for i in range(metadata.num_columns)]
    for index, name in enumerate(names):
        assert group.column(index).compression == GEOPARQUET_COMPRESSION.upper(), name
    for name in _DICTIONARY_COLUMNS:
        encodings = group.column(names.index(name)).encodings
        assert "RLE_DICTIONARY" in encodings, f"{name} lost dictionary encoding"


def test_dropping_rows_rebuilds_the_geo_bbox_from_the_rows_that_remain(
    tmp_path: Path,
) -> None:
    """A stale extent would claim coverage the published rows do not have."""
    far = _row(1, "   ")
    far["bbox_min_x"] = -50.0
    far["bbox_max_x"] = -40.0
    near = _row(2, "Kept description")

    data_root, parquet, _ = _prepare(tmp_path, [far, near])

    assert migrate_dataset_text(data_root) == 1

    geo = json.loads(pq.ParquetFile(parquet).schema_arrow.metadata[b"geo"])
    bbox = geo["columns"]["geometry"]["bbox"]
    assert bbox[0] == near["bbox_min_x"], "bbox still covers the dropped row"
    assert bbox[2] == near["bbox_max_x"]


def test_dropping_rows_preserves_metadata_inherited_from_the_source(
    tmp_path: Path,
) -> None:
    """Only the ``geo`` block is recomputed; other stored metadata is carried."""
    inherited = {b"provenance": b"kept", b"geo": b"{}"}
    schema = text_migration.SCHEMA.with_metadata(inherited)

    rebuilt = text_migration._geo_metadata_for(
        pa.Table.from_pylist([_row(1, "Kept")], schema=text_migration.SCHEMA),
        inherited,
    )

    assert rebuilt.metadata[b"provenance"] == b"kept"
    assert schema.metadata[b"provenance"] == b"kept"
