from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, call, patch

import pyarrow as pa
import pytest
from shapely import to_wkb
from shapely.geometry import MultiPolygon, Point, Polygon

import osm_polygon_description_tag.dataset.stats as stats_module
from osm_polygon_description_tag.dataset.manifest import ManifestError
from tests.helpers.dataset import write_reporting_fixture


def test_new_connection_uses_a_reentrant_disk_backed_temp_directory(
    tmp_path: Path,
) -> None:
    data_root = tmp_path / "generated"
    work_root = data_root / ".work" / "duckdb"
    connection = Mock()

    with patch.object(stats_module.duckdb, "connect", return_value=connection) as connect:
        assert stats_module._new_connection(data_root) is connection
        assert stats_module._new_connection(data_root) is connection

    assert work_root.is_dir()
    assert connect.call_args_list == [call(":memory:"), call(":memory:")]
    assert connection.execute.call_args_list == [
        call("SET temp_directory = ?", [str(work_root)]),
        call("SET temp_directory = ?", [str(work_root)]),
    ]


@pytest.mark.parametrize("row", [None, (None,)])
def test_quantile_or_none_returns_none_for_empty_or_null_results(row: object) -> None:
    connection = Mock()
    connection.execute.return_value.fetchone.return_value = row

    assert stats_module._quantile_or_none(connection, "area_m2", 0.5) is None


def test_quantile_or_none_rejects_other_columns_without_querying() -> None:
    connection = Mock()

    assert stats_module._quantile_or_none(connection, "rows", 0.5) is None
    connection.execute.assert_not_called()


def test_quantile_or_none_queries_area_and_returns_a_float() -> None:
    connection = Mock()
    connection.execute.return_value.fetchone.return_value = (4,)

    assert stats_module._quantile_or_none(connection, "area_m2", 0.5) == 4.0
    connection.execute.assert_called_once_with(
        "SELECT quantile_cont(area_m2, 0.5) FROM features WHERE area_m2 IS NOT NULL"
    )


def test_description_word_stats_handles_values_and_empty_results() -> None:
    connection = Mock()
    connection.execute.return_value.fetchone.side_effect = [(2, 5, 2.5), None]

    assert stats_module._description_word_stats(connection, localized=False) == (2, 5, 2.5)
    assert stats_module._description_word_stats(connection, localized=True) == (0, 0, None)

    base_query = connection.execute.call_args_list[0].args[0]
    assert "SELECT description AS value FROM features WHERE description IS NOT NULL" in base_query
    localized_query = connection.execute.call_args_list[1].args[0]
    assert "SELECT entry.value AS value" in localized_query


def test_map_sql_expression_supports_map_and_hub_list_representations() -> None:
    map_type = pa.map_(pa.string(), pa.string())
    list_type = pa.list_(
        pa.struct(
            [
                pa.field("key", pa.string()),
                pa.field("value", pa.string()),
            ]
        )
    )
    map_batch = pa.record_batch([pa.array([None], type=map_type)], names=["mapping"])
    list_batch = pa.record_batch([pa.array([None], type=list_type)], names=["mapping"])

    assert stats_module._map_sql_expression(map_batch, "mapping") == "mapping"
    assert stats_module._map_sql_expression(list_batch, "mapping") == "map_from_entries(mapping)"


def test_map_sql_expression_rejects_unsupported_mapping_types() -> None:
    struct_type = pa.struct([pa.field("value", pa.string())])
    batch = pa.record_batch([pa.array([None], type=struct_type)], names=["mapping"])

    with pytest.raises(stats_module.ReportingError, match="unsupported mapping representation"):
        stats_module._map_sql_expression(batch, "mapping")


@pytest.mark.parametrize(
    ("geometry", "expected"),
    [
        (
            Polygon(
                [(0, 0), (0, 4), (4, 4), (4, 0)],
                [[(1, 1), (1, 2), (2, 2), (2, 1)]],
            ),
            (8, 2, 1, 0),
        ),
        (
            MultiPolygon(
                [
                    Polygon([(0, 0), (0, 1), (1, 1), (1, 0)]),
                    Polygon([(2, 2), (2, 3), (3, 3), (3, 2)]),
                ]
            ),
            (8, 2, 0, 2),
        ),
    ],
)
def test_geometry_measurements_count_unique_vertices_rings_holes_and_parts(
    geometry: object,
    expected: tuple[int, int, int, int],
) -> None:
    assert (
        stats_module._geometry_measurements(
            to_wkb(geometry),  # type: ignore[arg-type]
            geometry.geom_type,  # type: ignore[attr-defined]
            source_name="region.parquet",
            row_index=0,
        )
        == expected
    )


def test_geometry_measurements_rejects_malformed_and_mismatched_geometry() -> None:
    with pytest.raises(stats_module.ReportingError, match="malformed geometry"):
        stats_module._geometry_measurements(
            b"not-wkb", "Polygon", source_name="region.parquet", row_index=4
        )


@pytest.mark.parametrize(
    "geometry",
    [
        Polygon(),
        Polygon([(0, 0), (1, 1), (0, 1), (1, 0), (0, 0)]),
    ],
)
def test_geometry_measurements_rejects_empty_and_invalid_geometry(geometry: Polygon) -> None:
    with pytest.raises(stats_module.ReportingError, match="invalid geometry"):
        stats_module._geometry_measurements(
            to_wkb(geometry), "Polygon", source_name="region.parquet", row_index=4
        )


def test_polygon_components_rejects_an_unexpected_geometry_class() -> None:
    with pytest.raises(stats_module.ReportingError, match="unsupported geometry"):
        stats_module._polygon_components(Point(0, 0), source_name="region.parquet", row_index=4)


@pytest.mark.parametrize("value", [None, True, "1", float("inf"), float("nan")])
def test_validated_area_rejects_nonfinite_and_non_numeric_values(value: object) -> None:
    with pytest.raises(stats_module.ReportingError, match="invalid area"):
        stats_module._validated_area(value, source_name="region.parquet", row_index=4)


def test_validated_geometry_type_rejects_missing_value() -> None:
    with pytest.raises(stats_module.ReportingError, match="missing geometry type"):
        stats_module._validated_geometry_type(None, source_name="region.parquet", row_index=4)


@pytest.mark.parametrize(
    "values",
    [
        (0.0, 1.0, 2.0),
        (0.0, 1.0, "bad", 2.0),
        (0.0, 1.0, float("inf"), 2.0),
    ],
)
def test_validated_bbox_rejects_wrong_length_non_numeric_and_nonfinite_values(
    values: tuple[object, ...],
) -> None:
    with pytest.raises(stats_module.ReportingError, match="invalid bounding box"):
        stats_module._validated_bbox(values, source_name="region.parquet", row_index=4)


def test_summarize_spatial_batch_rejects_missing_geometry() -> None:
    batch = pa.record_batch(
        [
            pa.array(["region.parquet"]),
            pa.array(["way"]),
            pa.array([42], type=pa.int64()),
            pa.array(["Polygon"]),
            pa.array([1.0], type=pa.float64()),
            pa.array([0.0], type=pa.float64()),
            pa.array([0.0], type=pa.float64()),
            pa.array([1.0], type=pa.float64()),
            pa.array([1.0], type=pa.float64()),
            pa.array([None], type=pa.binary()),
        ],
        names=stats_module._SPATIAL_COLUMNS,
    )

    with pytest.raises(stats_module.ReportingError, match="missing geometry"):
        stats_module._summarize_spatial_batch(batch, source_name="region.parquet", row_offset=4)

    with pytest.raises(stats_module.ReportingError, match="invalid geometry"):
        stats_module._geometry_measurements(
            to_wkb(Polygon([(0, 0), (0, 1), (1, 1), (1, 0)])),
            "MultiPolygon",
            source_name="region.parquet",
            row_index=4,
        )


def test_collect_spatial_summary_streams_all_files_and_is_empty_safe(tmp_path: Path) -> None:
    assert stats_module._collect_spatial_summary(()) == stats_module._SpatialSummary(
        rows=0,
        area_total_m2=0.0,
        area_mean_m2=None,
        dataset_bbox=None,
        geometry_vertices_total=0,
        geometry_rings_total=0,
        geometry_holes_total=0,
        multipolygon_components_total=0,
    )

    data_root = tmp_path / "generated"
    source_root = tmp_path / "raw"
    source_root.mkdir()
    write_reporting_fixture(data_root, source_root)
    artifacts = stats_module._find_validated_artifacts(data_root)

    summary = stats_module._collect_spatial_summary(artifacts)

    assert summary.rows == 3
    assert summary.area_total_m2 > 0
    assert summary.area_mean_m2 == pytest.approx(summary.area_total_m2 / 3)
    assert summary.dataset_bbox == (0.0, 0.0, 11.0, 11.0)
    assert summary.geometry_vertices_total == 12
    assert summary.geometry_rings_total == 3
    assert summary.geometry_holes_total == 0
    assert summary.multipolygon_components_total == 1


def test_spatial_area_total_does_not_depend_on_batch_order(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A running float total made stats.json differ in its last bits per run.

    The batch stream is not ordered by contract, so the dataset area must be
    summed exactly rather than accumulated as the batches happen to arrive.
    """
    data_root = tmp_path / "generated"
    source_root = tmp_path / "raw"
    source_root.mkdir()
    write_reporting_fixture(data_root, source_root)
    artifacts = stats_module._find_validated_artifacts(data_root)

    # Values chosen so that naive left-to-right accumulation is order-sensitive.
    areas = (1e16, 1.0, -1e16, 3.0)
    summaries = [
        stats_module._SpatialSummary(
            rows=1,
            area_total_m2=area,
            area_mean_m2=area,
            dataset_bbox=(0.0, 0.0, 1.0, 1.0),
            geometry_vertices_total=0,
            geometry_rings_total=0,
            geometry_holes_total=0,
            multipolygon_components_total=0,
        )
        for area in areas
    ]

    def collect(order: tuple[int, ...]) -> float:
        remaining = [summaries[index] for index in order]
        monkeypatch.setattr(
            stats_module,
            "iter_unique_parquet_batches",
            lambda *_args, **_kwargs: iter(range(len(remaining))),
        )
        monkeypatch.setattr(
            stats_module,
            "_summarize_spatial_batch",
            lambda batch, **_kwargs: remaining[batch],
        )
        return stats_module._collect_spatial_summary(artifacts).area_total_m2

    assert collect((0, 1, 2, 3)) == collect((2, 3, 0, 1)) == 4.0


def test_find_validated_artifacts_uses_explicit_legacy_text_mode(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    data_root = tmp_path / "generated"
    source_root = tmp_path / "raw"
    source_root.mkdir()
    write_reporting_fixture(data_root, source_root)
    calls: list[dict[str, object]] = []

    def record_validation(_path: Path, **kwargs: object) -> None:
        calls.append(kwargs)

    monkeypatch.setattr(stats_module, "validate_geoparquet", record_validation)

    artifacts = stats_module._find_validated_artifacts(data_root)

    assert len(artifacts) == 2
    assert calls == [{"require_successful_text": False}] * 2


def test_collect_spatial_summary_preserves_unique_source_name(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    parquet = tmp_path / "data" / "region.parquet"
    artifacts = (stats_module._ValidatedArtifact(parquet, Mock()),)
    batch = object()
    seen: list[tuple[object, str, int]] = []

    monkeypatch.setattr(
        stats_module,
        "iter_unique_parquet_batches",
        lambda *_args, **_kwargs: iter((batch,)),
    )

    def summarize(
        value: object, *, source_name: str, row_offset: int
    ) -> stats_module._SpatialSummary:
        seen.append((value, source_name, row_offset))
        return stats_module._SpatialSummary(0, 0.0, None, None, 0, 0, 0, 0)

    monkeypatch.setattr(stats_module, "_summarize_spatial_batch", summarize)

    stats_module._collect_spatial_summary(artifacts)

    assert seen == [(batch, "unique.parquet", 0)]


def test_create_unique_feature_view_requires_successful_text(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    connection = Mock()
    with patch.object(stats_module, "unique_rows_sql", return_value="SELECT 1") as builder:
        stats_module._create_unique_feature_view(connection)

    builder.assert_called_once_with(
        "all_features",
        stats_module._FEATURE_COLUMNS,
        key_value_columns_are_maps=True,
        require_successful_text=True,
    )
    connection.execute.assert_called_once_with("CREATE TEMP VIEW features AS SELECT 1")


def test_collect_spatial_summary_requests_strict_spatial_columns(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    parquet = tmp_path / "data" / "region.parquet"
    captured: dict[str, object] = {}

    def fake_iter(
        data_root: Path,
        *,
        columns: list[str],
        require_successful_text: bool,
    ) -> object:
        captured.update(
            data_root=data_root,
            columns=columns,
            require_successful_text=require_successful_text,
        )
        return iter(())

    monkeypatch.setattr(stats_module, "iter_unique_parquet_batches", fake_iter)
    artifacts = (stats_module._ValidatedArtifact(parquet, Mock()),)

    assert stats_module._collect_spatial_summary(artifacts).rows == 0
    assert captured == {
        "data_root": tmp_path,
        "columns": stats_module._SPATIAL_COLUMNS,
        "require_successful_text": True,
    }


def test_merge_bboxes_handles_empty_and_combines_minimums_and_maximums() -> None:
    first = (0.0, 2.0, 5.0, 8.0)
    second = (-1.0, 3.0, 7.0, 6.0)

    assert stats_module._merge_bboxes(None, None) is None
    assert stats_module._merge_bboxes(first, None) == first
    assert stats_module._merge_bboxes(None, second) == second
    assert stats_module._merge_bboxes(first, second) == (-1.0, 2.0, 7.0, 8.0)


def test_reporting_directories_requires_both_artifact_directories(tmp_path: Path) -> None:
    data_root = tmp_path / "generated"
    (data_root / "data").mkdir(parents=True)

    with pytest.raises(
        stats_module.ReportingError,
        match=rf"missing data/ or manifests/ under {data_root}",
    ):
        stats_module._reporting_directories(data_root)

    manifests_dir = data_root / "manifests"
    manifests_dir.mkdir()
    assert stats_module._reporting_directories(data_root) == (data_root / "data", manifests_dir)


def test_matching_parquets_is_sorted_by_filename_and_requires_matching_stems(
    tmp_path: Path,
) -> None:
    data_dir = tmp_path / "data"
    manifests_dir = tmp_path / "manifests"
    data_dir.mkdir()
    manifests_dir.mkdir()
    for name in ("b.parquet", "a.parquet"):
        (data_dir / name).write_bytes(b"")
    for name in ("b.manifest.json", "a.manifest.json"):
        (manifests_dir / name).write_text("{}", encoding="utf-8")

    parquets = stats_module._matching_parquets(data_dir, manifests_dir)
    assert [path.name for path in parquets] == ["a.parquet", "b.parquet"]

    (manifests_dir / "extra.manifest.json").write_text("{}", encoding="utf-8")
    with pytest.raises(
        stats_module.ReportingError,
        match=r"artifact/manifest mismatch \(missing or extra\): \['extra'\]",
    ):
        stats_module._matching_parquets(data_dir, manifests_dir)


def test_validate_artifact_wraps_manifest_errors_and_rejects_stale_outputs(
    tmp_path: Path,
) -> None:
    parquet = tmp_path / "region-a.parquet"
    manifests_dir = tmp_path / "manifests"
    manifests_dir.mkdir()

    with (
        patch.object(stats_module, "read_manifest", side_effect=ManifestError("bad json")),
        pytest.raises(
            stats_module.ReportingError,
            match=r"cannot read manifest for region-a: bad json",
        ),
    ):
        stats_module._validate_artifact(parquet, manifests_dir)

    manifest = SimpleNamespace(output="old")
    with (
        patch.object(stats_module, "read_manifest", return_value=manifest),
        patch.object(stats_module, "output_identity_for", return_value="new"),
        pytest.raises(
            stats_module.ReportingError,
            match=r"stale output identity for region-a\.parquet",
        ),
    ):
        stats_module._validate_artifact(parquet, manifests_dir)


def test_rows_in_parquet_handles_metadata_and_empty_metadata() -> None:
    path = Path("features.parquet")
    with patch.object(
        stats_module.pq,
        "ParquetFile",
        return_value=SimpleNamespace(metadata=SimpleNamespace(num_rows=17)),
    ):
        assert stats_module._rows_in_parquet(path) == 17

    with patch.object(
        stats_module.pq,
        "ParquetFile",
        return_value=SimpleNamespace(metadata=None),
    ):
        assert stats_module._rows_in_parquet(path) == 0


def test_query_int_defaults_only_when_the_query_has_no_row() -> None:
    connection = Mock()
    connection.execute.return_value.fetchone.side_effect = [(7,), None]

    assert stats_module._query_int(connection, "SELECT 7") == 7
    assert stats_module._query_int(connection, "SELECT NULL") == 0


def test_ingest_features_streams_the_allowlisted_columns_in_batches() -> None:
    artifact = SimpleNamespace(parquet=Path("region.parquet"))
    connection = Mock()
    reader = Mock()
    batches = [Mock(name="batch-a"), Mock(name="batch-b")]
    reader.iter_batches.return_value = batches

    with (
        patch.object(stats_module.pq, "ParquetFile", return_value=reader),
        patch.object(stats_module, "_insert_batch") as insert_batch,
    ):
        stats_module._ingest_features(connection, (artifact,))

    reader.iter_batches.assert_called_once_with(
        columns=stats_module._FEATURE_COLUMNS, batch_size=4096
    )
    assert insert_batch.call_args_list == [
        call(connection, batches[0], "region.parquet"),
        call(connection, batches[1], "region.parquet"),
    ]


def test_insert_batch_registers_and_unregisters_the_sql_batch_name() -> None:
    connection = Mock()
    batch = Mock()

    with patch.object(
        stats_module,
        "_map_sql_expression",
        side_effect=[
            "localized_names",
            "map_from_entries(localized_descriptions)",
            "tags",
        ],
    ):
        stats_module._insert_batch(connection, batch, "region.parquet")

    connection.register.assert_called_once_with("batch", batch)
    (query,) = connection.execute.call_args.args
    assert "FROM batch" in query
    connection.unregister.assert_called_once_with("batch")
