from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, call, patch

import pyarrow as pa
import pytest

import osm_polygon_description_tag.dataset.stats as stats_module
from osm_polygon_description_tag.dataset.manifest import ManifestError


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


def test_safe_map_runtime_conversion_is_independent_of_static_cast() -> None:
    assert stats_module._safe_map({"en": "3"}) == {"en": 3}


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
        side_effect=["localized_names", "map_from_entries(localized_descriptions)"],
    ):
        stats_module._insert_batch(connection, batch, "region.parquet")

    connection.register.assert_called_once_with("batch", batch)
    query, parameters = connection.execute.call_args.args
    assert "FROM batch" in query
    assert parameters == ["region.parquet"]
    connection.unregister.assert_called_once_with("batch")


def test_collect_feature_summary_forwards_each_query_and_quantile_contract() -> None:
    connection = Mock()
    min_timestamp = datetime(2024, 1, 2, tzinfo=UTC)
    max_timestamp = datetime(2024, 2, 3, tzinfo=UTC)
    connection.execute.return_value.fetchone.return_value = (min_timestamp, max_timestamp)

    with (
        patch.object(stats_module, "_query_int", side_effect=[11, 10, 3, 5, 4, 9]) as query_int,
        patch.object(
            stats_module,
            "_ordered_counts",
            side_effect=[{"relation": 4}, {"Polygon": 8}],
        ) as ordered_counts,
        patch.object(
            stats_module,
            "_suffix_counts",
            side_effect=[{"en": 6}, {"fr": 7}],
        ) as suffix_counts,
        patch.object(
            stats_module,
            "_description_word_stats",
            side_effect=[(3, 30, 10.0), (4, 40, 11.0)],
        ) as word_stats,
        patch.object(
            stats_module,
            "_quantile_or_none",
            side_effect=[1.0, 2.0, 3.0, 4.0, 5.0],
        ) as quantile,
    ):
        summary = stats_module._collect_feature_summary(connection)

    assert summary.rows == 11
    assert summary.unique_osm_objects == 10
    assert summary.osm_types == {"relation": 4}
    assert summary.geometry_types == {"Polygon": 8}
    assert summary.description_suffixes == {"en": 6}
    assert summary.name_suffixes == {"fr": 7}
    assert summary.base_description_rows == 3
    assert summary.localized_description_rows == 5
    assert summary.base_name_rows == 4
    assert summary.localized_name_rows == 9
    assert summary.base_description_values == 3
    assert summary.localized_description_values == 4
    assert summary.area_min_m2 == 1.0
    assert summary.area_p25_m2 == 2.0
    assert summary.area_median_m2 == 3.0
    assert summary.area_p75_m2 == 4.0
    assert summary.area_max_m2 == 5.0
    assert summary.data_min_timestamp_utc == min_timestamp.isoformat()
    assert summary.data_max_timestamp_utc == max_timestamp.isoformat()

    assert query_int.call_args_list == [
        call(connection, "SELECT COUNT(*) FROM features"),
        call(connection, "SELECT COUNT(*) FROM (SELECT DISTINCT osm_type, osm_id FROM features)"),
        call(connection, "SELECT COUNT(*) FROM features WHERE description IS NOT NULL"),
        call(
            connection,
            "SELECT COUNT(*) FROM features WHERE cardinality(localized_descriptions) > 0",
        ),
        call(connection, "SELECT COUNT(*) FROM features WHERE name IS NOT NULL"),
        call(connection, "SELECT COUNT(*) FROM features WHERE cardinality(localized_names) > 0"),
    ]
    assert ordered_counts.call_args_list == [
        call(
            connection,
            "SELECT osm_type, COUNT(*) FROM features GROUP BY osm_type ORDER BY osm_type",
        ),
        call(
            connection,
            "SELECT geometry_type, COUNT(*) FROM features GROUP BY geometry_type "
            "ORDER BY geometry_type",
        ),
    ]
    connection.execute.assert_called_once_with(
        "SELECT MIN(timestamp), MAX(timestamp) FROM features WHERE timestamp IS NOT NULL"
    )
    assert suffix_counts.call_args_list == [
        call(connection, "localized_descriptions"),
        call(connection, "localized_names"),
    ]
    assert word_stats.call_args_list == [
        call(connection, localized=False),
        call(connection, localized=True),
    ]
    assert quantile.call_args_list == [
        call(connection, "area_m2", 0.0),
        call(connection, "area_m2", 0.25),
        call(connection, "area_m2", 0.5),
        call(connection, "area_m2", 0.75),
        call(connection, "area_m2", 1.0),
    ]


def test_collect_manifest_summary_accumulates_and_sorts_file_provenance(
    tmp_path: Path,
) -> None:
    first = tmp_path / "b.parquet"
    second = tmp_path / "a.parquet"
    first.write_bytes(b"b" * 5)
    second.write_bytes(b"a" * 3)

    def artifact(
        path: Path, source_name: str, source_size: int, emitted: int, rejections: dict[str, int]
    ):
        manifest = SimpleNamespace(
            source=SimpleNamespace(
                name=source_name, size_bytes=source_size, sha256=f"sha-{source_name}"
            ),
            counts=SimpleNamespace(emitted_features=emitted, rejections=rejections),
        )
        return stats_module._ValidatedArtifact(parquet=path, manifest=manifest)

    artifacts = (
        artifact(first, "b.osm.pbf", 20, 3, {"z": 1}),
        artifact(second, "a.osm.pbf", 10, 4, {"a": 2}),
    )

    with (
        patch.object(stats_module, "_rows_in_parquet", side_effect=[6, 4]),
        patch.object(stats_module, "file_sha256", side_effect=["out-b", "out-a"]),
    ):
        summary = stats_module._collect_manifest_summary(artifacts)

    assert summary.emitted_features == 7
    assert summary.rejections == {"a": 2, "z": 1}
    assert summary.source_bytes_total == 30
    assert summary.output_bytes_total == 8
    assert summary.files == [
        {
            "source_pbf": "a.osm.pbf",
            "parquet": "a.parquet",
            "rows": 4,
            "source_bytes": 10,
            "output_bytes": 3,
            "emitted_features": 4,
            "rejections": {"a": 2},
            "source_sha256": "sha-a.osm.pbf",
            "output_sha256": "out-a",
        },
        {
            "source_pbf": "b.osm.pbf",
            "parquet": "b.parquet",
            "rows": 6,
            "source_bytes": 20,
            "output_bytes": 5,
            "emitted_features": 3,
            "rejections": {"z": 1},
            "source_sha256": "sha-b.osm.pbf",
            "output_sha256": "out-b",
        },
    ]


def test_build_stats_payload_preserves_public_fields_and_zero_rate_fallback() -> None:
    feature_summary = stats_module._FeatureSummary(
        rows=10,
        unique_osm_objects=7,
        osm_types={"relation": 4},
        geometry_types={"Polygon": 6},
        description_suffixes={"en": 3},
        name_suffixes={"fr": 2},
        base_description_rows=5,
        localized_description_rows=6,
        base_description_values=5,
        base_description_words_total=20,
        base_description_words_median=4.0,
        localized_description_values=6,
        localized_description_words_total=24,
        localized_description_words_median=4.0,
        base_name_rows=7,
        localized_name_rows=8,
        area_min_m2=1.0,
        area_p25_m2=2.0,
        area_median_m2=3.0,
        area_p75_m2=4.0,
        area_max_m2=5.0,
        data_min_timestamp_utc="2024-01-01T00:00:00+00:00",
        data_max_timestamp_utc="2024-02-01T00:00:00+00:00",
    )
    manifest_summary = stats_module._ManifestSummary(
        emitted_features=12,
        rejections={"duplicate_osm_object": 2},
        source_bytes_total=30,
        output_bytes_total=8,
        files=[{"parquet": "a.parquet"}],
    )

    payload = stats_module._build_stats_payload(feature_summary, manifest_summary)

    assert payload["regional_overlap_duplicate_rows"] == 3
    assert payload["regional_overlap_duplicate_rate"] == 0.3
    assert payload["deduplicated_rows"] == 2
    assert payload["source_bytes_total"] == 30
    assert payload["output_bytes_total"] == 8
    assert payload["area_m2_count"] == 10
    assert payload["area_m2_p25_m2"] == 2.0
    assert payload["area_m2_p75_m2"] == 4.0
    assert payload["files"] == [{"parquet": "a.parquet"}]

    no_duplicate_manifest_summary = stats_module._ManifestSummary(
        emitted_features=12,
        rejections={"other": 1},
        source_bytes_total=30,
        output_bytes_total=8,
        files=[{"parquet": "a.parquet"}],
    )
    assert (
        stats_module._build_stats_payload(feature_summary, no_duplicate_manifest_summary)[
            "deduplicated_rows"
        ]
        == 0
    )

    empty_feature_summary = stats_module._FeatureSummary(
        **{**feature_summary.__dict__, "rows": 0, "unique_osm_objects": 0}
    )
    assert (
        stats_module._build_stats_payload(empty_feature_summary, manifest_summary)[
            "regional_overlap_duplicate_rate"
        ]
        == 0.0
    )
