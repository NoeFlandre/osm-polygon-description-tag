"""Behavioral coverage for the bounded GeoParquet validation helpers."""

import json
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, call, patch

import pyarrow as pa
import pytest
from shapely import to_wkb
from shapely.geometry import Polygon

import osm_polygon_description_tag.dataset.storage as storage
from osm_polygon_description_tag.dataset.manifest import ManifestError
from osm_polygon_description_tag.dataset.storage import (
    StorageError,
    _arrow_record,
    _batch_columns,
    _check_artifact_stems,
    _check_field,
    _decode_geometry,
    _fsync_dir,
    _geometry_metadata_column,
    _merge_bounds,
    _read_geo_metadata,
    _record_bounds,
    _RecordStreamSummary,
    _require_artifact_directories,
    _require_batch_size,
    _stream_records,
    _stream_rewrite_with_metadata,
    _UniquenessIndex,
    _validate_area,
    _validate_bbox,
    _validate_finalized_artifacts_strict,
    _validate_geo_metadata_header,
    _validate_geometry,
    _validate_geometry_metadata_column,
    _validate_geometry_type,
    _validate_manifest_pair,
    _validate_metadata_bbox,
    _validate_metadata_extent,
    _validate_row,
    _validate_source,
    _ValidationState,
    validate_finalized_artifacts,
    write_geoparquet,
)


def _columns(row: dict[str, object]) -> dict[str, list[object]]:
    return {
        name: [row[name]]
        for name in (
            "source_pbf",
            "osm_type",
            "osm_id",
            "geometry_type",
            "area_m2",
            "bbox_min_x",
            "bbox_min_y",
            "bbox_max_x",
            "bbox_max_y",
            "geometry",
        )
    }


def test_arrow_record_preserves_scalars_and_normalizes_key_value_columns() -> None:
    record = {
        "source_pbf": "region.osm.pbf",
        "localized_descriptions": {"fr": "Bonjour"},
        "tags": {"description": "Hello"},
    }

    assert _arrow_record(record) == {
        "source_pbf": "region.osm.pbf",
        "localized_descriptions": [{"key": "fr", "value": "Bonjour"}],
        "localized_names": [],
        "tags": [{"key": "description", "value": "Hello"}],
    }


def test_record_bounds_reads_all_four_coordinates_as_floats() -> None:
    record = {
        "bbox_min_x": "-1.5",
        "bbox_min_y": 2,
        "bbox_max_x": 3.25,
        "bbox_max_y": "4.75",
    }

    assert _record_bounds(record) == (-1.5, 2.0, 3.25, 4.75)


def test_merge_bounds_handles_first_update_and_each_extent_direction() -> None:
    first = (10.0, 20.0, 30.0, 40.0)
    second = (15.0, 5.0, 35.0, 45.0)

    assert _merge_bounds(None, first) == first
    assert _merge_bounds(first, second) == (10.0, 5.0, 35.0, 45.0)


class _BatchRecorder:
    def __init__(self) -> None:
        self.batches: list[pa.RecordBatch] = []

    def write_batch(self, batch: pa.RecordBatch) -> None:
        self.batches.append(batch)


def test_stream_records_reports_summary_and_writes_exact_batch_sizes(
    valid_records: list[dict[str, object]],
) -> None:
    writer = _BatchRecorder()

    summary = _stream_records(iter(valid_records), writer, batch_size=1)

    assert summary.row_count == len(valid_records)
    assert summary.geometry_types == frozenset({"Polygon", "MultiPolygon"})
    assert summary.bbox == (0.0, 0.0, 21.0, 21.0)
    assert [batch.num_rows for batch in writer.batches] == [1, 1]


def test_stream_records_writes_an_empty_schema_batch_for_empty_input() -> None:
    writer = _BatchRecorder()

    summary = _stream_records(iter(()), writer, batch_size=1)

    assert summary.row_count == 0
    assert summary.geometry_types == frozenset()
    assert summary.bbox is None
    assert [batch.num_rows for batch in writer.batches] == [0]


@pytest.mark.parametrize("batch_size", [0, -1])
def test_require_batch_size_rejects_non_positive_values(batch_size: int) -> None:
    with pytest.raises(ValueError, match="batch_size must be positive"):
        _require_batch_size(batch_size)


def test_require_batch_size_uses_the_exact_error_message() -> None:
    with pytest.raises(ValueError) as error:
        _require_batch_size(0)

    assert str(error.value) == "batch_size must be positive"


def test_require_batch_size_accepts_one() -> None:
    _require_batch_size(1)


def test_validate_row_records_identity_extent_and_geometry(
    tmp_path: Path, way_record_dict: dict[str, object]
) -> None:
    with _UniquenessIndex(work_root=tmp_path / "work") as uniqueness:
        state = _ValidationState(uniqueness=uniqueness)

        _validate_row(_columns(way_record_dict), 0, state)

        assert state.source_pbf == "region.osm.pbf"
        assert state.actual_types == {"Polygon"}
        assert state.row_count == 1
        assert (state.min_x, state.min_y, state.max_x, state.max_y) == (0.0, 0.0, 1.0, 1.0)


def test_validate_row_counts_each_valid_row(
    tmp_path: Path, valid_records: list[dict[str, object]]
) -> None:
    with _UniquenessIndex(work_root=tmp_path / "work") as uniqueness:
        state = _ValidationState(uniqueness=uniqueness)

        _validate_row(_columns(valid_records[0]), 0, state)
        _validate_row(_columns(valid_records[1]), 0, state)

        assert state.row_count == 2
        assert state.actual_types == {"Polygon", "MultiPolygon"}


def test_validate_row_rejects_duplicate_identity_without_counting_it(
    tmp_path: Path, way_record_dict: dict[str, object]
) -> None:
    with _UniquenessIndex(work_root=tmp_path / "work") as uniqueness:
        state = _ValidationState(uniqueness=uniqueness)
        columns = _columns(way_record_dict)

        _validate_row(columns, 0, state)
        with pytest.raises(StorageError, match="duplicate"):
            _validate_row(columns, 0, state)

        assert state.row_count == 1


@pytest.mark.parametrize(
    ("values", "message"),
    [
        ((None, 0.0, 1.0, 1.0), "non-finite bbox_min_x: None"),
        ((0.0, None, 1.0, 1.0), "non-finite bbox_min_y: None"),
        ((0.0, 0.0, None, 1.0), "non-finite bbox_max_x: None"),
        ((0.0, 0.0, 1.0, None), "non-finite bbox_max_y: None"),
        ((0.0, 0.0, float("nan"), 1.0), "non-finite bbox_max_x: nan"),
        ((2.0, 0.0, 0.0, 1.0), "bbox min coordinate exceeds max"),
        ((0.0, 2.0, 1.0, 1.0), "bbox min coordinate exceeds max"),
    ],
)
def test_validate_bbox_rejects_invalid_coordinates_or_order(
    tmp_path: Path,
    values: tuple[float | None, float | None, float | None, float | None],
    message: str,
) -> None:
    with _UniquenessIndex(work_root=tmp_path / "work") as uniqueness:
        state = _ValidationState(uniqueness=uniqueness)
        with pytest.raises(StorageError) as error:
            _validate_bbox(state, values)
        assert str(error.value) == message


def test_validate_bbox_accumulates_global_extent(tmp_path: Path) -> None:
    with _UniquenessIndex(work_root=tmp_path / "work") as uniqueness:
        state = _ValidationState(uniqueness=uniqueness)

        _validate_bbox(state, (2.0, 3.0, 4.0, 5.0))
        _validate_bbox(state, (-1.0, 1.0, 10.0, 6.0))

        assert (state.min_x, state.min_y, state.max_x, state.max_y) == (
            -1.0,
            1.0,
            10.0,
            6.0,
        )


@pytest.mark.parametrize("values", [(0.0, 0.0, 0.0, 1.0), (0.0, 0.0, 1.0, 0.0)])
def test_validate_bbox_allows_zero_width_or_height(
    tmp_path: Path, values: tuple[float, float, float, float]
) -> None:
    with _UniquenessIndex(work_root=tmp_path / "work") as uniqueness:
        state = _ValidationState(uniqueness=uniqueness)

        _validate_bbox(state, values)

        assert state.row_count == 0


def test_validate_bbox_rejects_wrong_arity(tmp_path: Path) -> None:
    with _UniquenessIndex(work_root=tmp_path / "work") as uniqueness:
        state = _ValidationState(uniqueness=uniqueness)

        with pytest.raises(ValueError, match=r"zip\(\) argument 2 is shorter"):
            _validate_bbox(state, (0.0, 0.0, 1.0, 1.0, 2.0))  # type: ignore[arg-type]


def test_validate_metadata_extent_checks_types_and_bbox(tmp_path: Path) -> None:
    with _UniquenessIndex(work_root=tmp_path / "work") as uniqueness:
        state = _ValidationState(
            uniqueness=uniqueness,
            actual_types={"Polygon"},
            min_x=0.0,
            min_y=1.0,
            max_x=2.0,
            max_y=3.0,
            row_count=1,
        )

        _validate_metadata_extent(state, {"Polygon"}, [0.0, 1.0, 2.0, 3.0])

        with pytest.raises(StorageError) as mismatch:
            _validate_metadata_extent(state, {"MultiPolygon"}, [0.0, 1.0, 2.0, 3.0])
        assert str(mismatch.value) == (
            "geometry_types mismatch: actual ['Polygon'] != metadata ['MultiPolygon']"
        )
        with pytest.raises(StorageError, match="bbox mismatch"):
            _validate_metadata_extent(state, {"Polygon"}, [0.0, 1.0, 2.0, 4.0])


def test_validate_metadata_extent_ignores_bbox_for_empty_files(tmp_path: Path) -> None:
    with _UniquenessIndex(work_root=tmp_path / "work") as uniqueness:
        state = _ValidationState(uniqueness=uniqueness, row_count=0)

        _validate_metadata_extent(state, set(), None)
        _validate_metadata_extent(state, set(), [0.0, 0.0, 1.0, 1.0])


def test_check_field_accepts_legacy_key_value_maps_but_rejects_other_maps() -> None:
    legacy_map = pa.map_(pa.string(), pa.string())

    _check_field(pa.field("tags", legacy_map, nullable=False), "tags")

    with pytest.raises(StorageError, match="field type mismatch for source_pbf"):
        _check_field(pa.field("source_pbf", legacy_map, nullable=False), "source_pbf")


def test_check_field_rejects_nullability_drift_with_exact_message() -> None:
    with pytest.raises(StorageError) as error:
        _check_field(pa.field("source_pbf", pa.string(), nullable=True), "source_pbf")

    assert str(error.value) == "field nullability mismatch for source_pbf"


@pytest.mark.parametrize(
    ("metadata", "message"),
    [
        (
            {"version": "1.0.0", "primary_column": "geometry"},
            "unsupported GeoParquet version: '1.0.0'",
        ),
        (
            {"version": "1.1.0", "primary_column": "geom"},
            "primary geometry column must be 'geometry'",
        ),
    ],
)
def test_validate_geo_metadata_header_rejects_each_contract_field(
    metadata: dict[str, object], message: str
) -> None:
    with pytest.raises(StorageError) as error:
        _validate_geo_metadata_header(metadata)

    assert str(error.value) == message


def test_geometry_metadata_column_requires_a_mapping_and_geometry_entry() -> None:
    with pytest.raises(StorageError) as missing_from_list:
        _geometry_metadata_column({"columns": ["geometry"]})
    assert str(missing_from_list.value) == "missing 'geometry' column metadata"

    with pytest.raises(StorageError) as missing_from_mapping:
        _geometry_metadata_column({"columns": {}})
    assert str(missing_from_mapping.value) == "missing 'geometry' column metadata"

    with pytest.raises(StorageError) as wrong_type:
        _geometry_metadata_column({"columns": {"geometry": []}})
    assert str(wrong_type.value) == "geometry metadata must be an object"


def test_read_geo_metadata_reports_the_exact_missing_metadata_error() -> None:
    with pytest.raises(StorageError) as error:
        _read_geo_metadata(pa.schema([], metadata={}))

    assert str(error.value) == "missing GeoParquet 'geo' metadata"


def test_validate_geometry_metadata_column_requires_wkb_exactly() -> None:
    _validate_geometry_metadata_column({"encoding": "WKB"})

    with pytest.raises(StorageError) as error:
        _validate_geometry_metadata_column({"encoding": "WKT"})

    assert str(error.value) == "geometry encoding must be WKB"


def test_uniqueness_index_accepts_an_existing_root_and_uses_exact_db_name(
    tmp_path: Path,
) -> None:
    work_root = tmp_path / "work"
    work_root.mkdir()

    with _UniquenessIndex(work_root=work_root) as index:
        assert index.db_path.name == "uniqueness.db"


def test_uniqueness_index_close_is_idempotent_and_suppresses_non_empty_root(
    tmp_path: Path,
) -> None:
    work_root = tmp_path / "work"
    keep = work_root / "keep.txt"
    work_root.mkdir()
    keep.write_text("keep", encoding="utf-8")
    index = _UniquenessIndex(work_root=work_root)

    index.close()
    index.close()

    assert index._closed is True
    assert keep.is_file()


def test_uniqueness_index_close_always_ignores_rmtree_errors(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    index = _UniquenessIndex(work_root=tmp_path / "work")
    calls: list[object] = []

    def fail_unless_ignored(path: Path, *args: object, **kwargs: object) -> None:
        del path, args
        calls.append(kwargs.get("ignore_errors"))
        if kwargs.get("ignore_errors") is not True:
            raise OSError("cleanup must be best effort")

    monkeypatch.setattr("shutil.rmtree", fail_unless_ignored)
    index.close()

    assert calls == [True]


def test_check_artifact_stems_requires_matching_suffixes() -> None:
    _check_artifact_stems(
        [Path("a.parquet"), Path("b.parquet")],
        [
            Path("a.manifest.json"),
            Path("b.manifest.json"),
        ],
    )

    with pytest.raises(StorageError, match="artifact/manifest mismatch"):
        _check_artifact_stems([Path("a.parquet")], [Path("b.manifest.json")])


@pytest.mark.parametrize("missing", ["data", "manifests"])
def test_require_artifact_directories_reports_the_missing_directory(
    tmp_path: Path, missing: str
) -> None:
    data_dir = tmp_path / "data"
    manifests_dir = tmp_path / "manifests"
    if missing == "data":
        manifests_dir.mkdir()
        expected = f"missing data directory: {data_dir}"
    else:
        data_dir.mkdir()
        expected = f"missing manifests directory: {manifests_dir}"

    with pytest.raises(StorageError) as error:
        _require_artifact_directories(data_dir, manifests_dir)

    assert str(error.value) == expected


def test_validate_manifest_pair_reads_the_expected_path_and_checks_identity(
    tmp_path: Path,
) -> None:
    parquet = tmp_path / "region.parquet"
    manifests_dir = tmp_path / "manifests"
    manifests_dir.mkdir()
    manifest_path = manifests_dir / "region.manifest.json"
    expected_identity = object()
    manifest = SimpleNamespace(
        manifest_schema_version=storage.MANIFEST_SCHEMA_VERSION,
        output=expected_identity,
    )

    with (
        patch.object(storage, "read_manifest", return_value=manifest) as read_manifest,
        patch.object(
            storage, "output_identity_for", return_value=expected_identity
        ) as output_identity,
    ):
        assert _validate_manifest_pair(parquet, manifests_dir) == manifest_path

    read_manifest.assert_called_once_with(manifest_path)
    output_identity.assert_called_once_with(parquet)


def test_validate_manifest_pair_wraps_invalid_manifest_errors(tmp_path: Path) -> None:
    parquet = tmp_path / "region.parquet"
    manifests_dir = tmp_path / "manifests"
    manifests_dir.mkdir()
    manifest_path = manifests_dir / "region.manifest.json"

    with (
        patch.object(storage, "read_manifest", side_effect=ManifestError("broken")),
        pytest.raises(StorageError) as error,
    ):
        _validate_manifest_pair(parquet, manifests_dir)

    assert str(error.value) == f"invalid manifest {manifest_path}: broken"
    assert isinstance(error.value.__cause__, ManifestError)


@pytest.mark.parametrize(
    ("manifest_version", "output", "message"),
    [
        (999, object(), "manifest uses unsupported schema version: 999"),
        (storage.MANIFEST_SCHEMA_VERSION, object(), "stale output identity for region.parquet"),
    ],
)
def test_validate_manifest_pair_rejects_unsupported_or_stale_manifests(
    tmp_path: Path,
    manifest_version: int,
    output: object,
    message: str,
) -> None:
    parquet = tmp_path / "region.parquet"
    manifests_dir = tmp_path / "manifests"
    manifests_dir.mkdir()
    manifest = SimpleNamespace(manifest_schema_version=manifest_version, output=output)

    with (
        patch.object(storage, "read_manifest", return_value=manifest),
        patch.object(storage, "output_identity_for", return_value=object()),
        pytest.raises(StorageError) as error,
    ):
        _validate_manifest_pair(parquet, manifests_dir)

    assert str(error.value) == message


def test_validate_finalized_artifacts_returns_sorted_pairs_and_validates_each(
    tmp_path: Path,
) -> None:
    data_dir = tmp_path / "data"
    manifests_dir = tmp_path / "manifests"
    data_dir.mkdir()
    manifests_dir.mkdir()
    for name in ("b", "a"):
        (data_dir / f"{name}.parquet").write_bytes(b"")
        (manifests_dir / f"{name}.manifest.json").write_bytes(b"")

    validated = [manifests_dir / "a.manifest.json", manifests_dir / "b.manifest.json"]
    with patch.object(storage, "_validate_manifest_pair", side_effect=validated) as check:
        result = validate_finalized_artifacts(tmp_path)

    assert result == {
        "parquets": (data_dir / "a.parquet", data_dir / "b.parquet"),
        "manifests": tuple(validated),
    }
    assert check.call_args_list == [
        call(data_dir / "a.parquet", manifests_dir),
        call(data_dir / "b.parquet", manifests_dir),
    ]


def test_validate_finalized_artifacts_strict_validates_every_parquet(
    tmp_path: Path,
) -> None:
    result = {
        "parquets": (tmp_path / "a.parquet", tmp_path / "b.parquet"),
        "manifests": (),
    }
    with (
        patch.object(storage, "validate_finalized_artifacts", return_value=result) as base,
        patch.object(storage, "validate_geoparquet") as strict,
    ):
        assert _validate_finalized_artifacts_strict(tmp_path) is result

    base.assert_called_once_with(tmp_path)
    assert strict.call_args_list == [
        call(tmp_path / "a.parquet"),
        call(tmp_path / "b.parquet"),
    ]


def test_batch_columns_materializes_all_validation_columns(
    way_record_dict: dict[str, object],
) -> None:
    batch = pa.RecordBatch.from_pylist([_arrow_record(way_record_dict)], schema=storage.SCHEMA)

    columns = _batch_columns(batch)

    assert tuple(columns) == tuple(storage._VALIDATION_COLUMNS)
    assert columns["osm_id"] == [100]
    assert columns["geometry"] == [way_record_dict["geometry"]]


def test_validate_geoparquet_uses_empty_metadata_defaults_and_exact_validation_inputs(
    tmp_path: Path,
) -> None:
    data_root = tmp_path / "dataset"
    parquet = data_root / "data" / "region.parquet"
    parquet.parent.mkdir(parents=True)
    parquet.touch()
    parquet_file = Mock()
    parquet_file.schema_arrow = object()
    parquet_file.iter_batches.return_value = []
    uniqueness = Mock()

    with (
        patch.object(storage.pq, "ParquetFile", return_value=parquet_file),
        patch.object(storage, "_check_schema") as check_schema,
        patch.object(
            storage,
            "_read_geo_metadata",
            return_value={"columns": {"geometry": {}}},
        ),
        patch.object(storage, "_UniquenessIndex", return_value=uniqueness) as index_factory,
    ):
        assert storage.validate_geoparquet(parquet) == 0

    check_schema.assert_called_once_with(parquet_file.schema_arrow)
    index_factory.assert_called_once_with(work_root=data_root / ".work" / "validation")
    parquet_file.iter_batches.assert_called_once_with(columns=storage._VALIDATION_COLUMNS)
    uniqueness.close.assert_called_once_with()


def test_fsync_dir_uses_the_owned_directory_and_closes_the_fd(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    opened: list[tuple[object, object]] = []
    synced: list[int] = []
    closed: list[int] = []

    monkeypatch.setattr(
        storage.os,
        "open",
        lambda path, flags: opened.append((path, flags)) or 17,
    )
    monkeypatch.setattr(storage.os, "fsync", lambda fd: synced.append(fd))
    monkeypatch.setattr(storage.os, "close", lambda fd: closed.append(fd))

    _fsync_dir(tmp_path)

    assert opened == [(str(tmp_path), storage.os.O_RDONLY)]
    assert synced == [17]
    assert closed == [17]


def test_write_geoparquet_uses_contract_writer_options_and_default_batch_size(
    tmp_path: Path,
) -> None:
    target = tmp_path / "region.parquet"
    temp_data = tmp_path / ".region.data.tmp"
    temp_final = tmp_path / ".region.final.tmp"
    records = object()
    summary = _RecordStreamSummary(0, frozenset(), None)
    writer = Mock()
    writer.__enter__ = Mock(return_value=writer)
    writer.__exit__ = Mock(return_value=None)
    validator = Mock(return_value=7)

    with (
        patch.object(storage, "_owned_temp", side_effect=[temp_data, temp_final]),
        patch.object(storage.pq, "ParquetWriter", return_value=writer) as writer_factory,
        patch.object(storage, "_stream_records", return_value=summary) as stream,
        patch.object(storage, "_stream_rewrite_with_metadata") as rewrite,
        patch.object(storage, "_fsync_path") as fsync_path,
        patch.object(storage, "_fsync_dir") as fsync_dir,
        patch.object(storage.os, "replace") as replace,
    ):
        assert write_geoparquet(records, target, validator=validator) == 7

    writer_factory.assert_called_once_with(
        temp_data,
        storage.SCHEMA,
        compression="zstd",
        use_dictionary=storage._DICTIONARY_COLUMNS,
    )
    stream.assert_called_once_with(records, writer, 1024)
    rewrite.assert_called_once_with(
        temp_data,
        temp_final,
        geometry_types=[],
        bbox=[],
    )
    validator.assert_called_once_with(temp_final)
    fsync_path.assert_called_once_with(temp_final)
    fsync_dir.assert_called_once_with(tmp_path)
    replace.assert_called_once_with(temp_final, target)


def test_stream_rewrite_passes_exact_metadata_writer_options(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    source = tmp_path / "source.parquet"
    target = tmp_path / "target.parquet"
    batch = object()
    reader = Mock()
    reader.iter_batches.return_value = [batch]
    writer = Mock()
    writer.__enter__ = Mock(return_value=writer)
    writer.__exit__ = Mock(return_value=None)
    writer_factory = Mock(return_value=writer)
    reader_factory = Mock(return_value=reader)
    monkeypatch.setattr(storage.pq, "ParquetFile", reader_factory)
    monkeypatch.setattr(storage.pq, "ParquetWriter", writer_factory)

    _stream_rewrite_with_metadata(
        source,
        target,
        geometry_types=["Polygon"],
        bbox=[0.0, 1.0, 2.0, 3.0],
    )

    reader_factory.assert_called_once_with(source)
    writer_factory.assert_called_once()
    writer_args, writer_kwargs = writer_factory.call_args
    assert writer_args[0] == target
    assert json.loads(writer_args[1].metadata[b"geo"]) == {
        "version": "1.1.0",
        "primary_column": "geometry",
        "columns": {
            "geometry": {
                "encoding": "WKB",
                "geometry_types": ["Polygon"],
                "bbox": [0.0, 1.0, 2.0, 3.0],
                "covering": {
                    "bbox": {
                        "xmin": ["bbox_min_x"],
                        "ymin": ["bbox_min_y"],
                        "xmax": ["bbox_max_x"],
                        "ymax": ["bbox_max_y"],
                    }
                },
            }
        },
    }
    assert writer_kwargs == {"compression": "zstd", "use_dictionary": storage._DICTIONARY_COLUMNS}
    reader.iter_batches.assert_called_once_with(batch_size=4096)
    writer.write_batch.assert_called_once_with(batch)


def test_validate_source_geometry_type_and_area_use_exact_error_messages(
    tmp_path: Path,
) -> None:
    with _UniquenessIndex(work_root=tmp_path / "work") as uniqueness:
        state = _ValidationState(uniqueness=uniqueness, source_pbf="a.osm.pbf")
        with pytest.raises(StorageError) as source_error:
            _validate_source(state, "b.osm.pbf")
        assert str(source_error.value) == (
            "mixed source_pbf within file: 'a.osm.pbf' and 'b.osm.pbf'"
        )

        with pytest.raises(StorageError) as geometry_type_error:
            _validate_geometry_type(state, "LineString")
        assert str(geometry_type_error.value) == "unsupported geometry_type: 'LineString'"

    with pytest.raises(StorageError) as area_error:
        _validate_area(None)
    assert str(area_error.value) == "non-positive or non-finite area_m2: None"


def test_geometry_validation_rejects_null_invalid_and_undecodable_values() -> None:
    with pytest.raises(StorageError, match="^null geometry$"):
        _validate_geometry(None, "Polygon")

    invalid = to_wkb(Polygon([(0, 0), (1, 1), (1, 0), (0, 1), (0, 0)]))
    with pytest.raises(StorageError, match="geometry_type/WKB mismatch or invalid geometry"):
        _validate_geometry(invalid, "Polygon")

    with pytest.raises(StorageError) as decode_error:
        _decode_geometry(b"not-wkb")
    assert str(decode_error.value).startswith("undecodable WKB geometry:")


def test_validate_metadata_bbox_checks_arity_and_exact_tolerance(
    tmp_path: Path,
) -> None:
    with _UniquenessIndex(work_root=tmp_path / "work") as uniqueness:
        state = _ValidationState(
            uniqueness=uniqueness,
            min_x=0.0,
            min_y=1.0,
            max_x=2.0,
            max_y=3.0,
        )
        _validate_metadata_bbox(state, [1e-9, 1.0, 2.0, 3.0])
        with pytest.raises(ValueError, match=r"zip\(\) argument 2 is shorter"):
            _validate_metadata_bbox(state, [0.0, 1.0, 2.0])

        with pytest.raises(StorageError) as mismatch:
            _validate_metadata_bbox(state, [0.0, 1.0, 2.1, 3.0])
        assert str(mismatch.value).startswith("bbox mismatch: actual")
