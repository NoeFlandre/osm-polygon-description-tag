"""Unit tests for deterministic Parquet relation construction."""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import pytest
from shapely import to_wkb
from shapely.geometry import Polygon

import osm_polygon_description_tag.dataset.unique_rows as unique_rows
from osm_polygon_description_tag.dataset.canonical_rows import (
    canonical_geometry_wkb,
    select_canonical_row,
)
from tests.conftest import make_record_dict
from tests.helpers.dataset import write_finalized_dataset
from tests.helpers.messages import exactly


def test_parquet_column_expression_normalizes_geoparquet_geometry() -> None:
    assert (
        unique_rows._parquet_column_expression(
            "geometry",
            {"geometry"},
            has_geo_metadata=True,
            path=Path("region.parquet"),
        )
        == "ST_AsWKB(ST_GeomFromWKB(ST_AsWKB(geometry))) AS geometry"
    )


@pytest.mark.parametrize(
    ("column", "available", "has_geo_metadata", "expected"),
    [
        (
            "geometry",
            {"geometry"},
            False,
            "ST_AsWKB(ST_GeomFromWKB(geometry)) AS geometry",
        ),
        ("version", set(), False, "CAST(NULL AS INTEGER) AS version"),
        ("timestamp", set(), False, "CAST(NULL AS TIMESTAMP) AS timestamp"),
        ("description", set(), False, "CAST(NULL AS VARCHAR) AS description"),
    ],
)
def test_parquet_column_expression_handles_non_geospatial_and_optional_columns(
    column: str,
    available: set[str],
    has_geo_metadata: bool,
    expected: str,
) -> None:
    assert (
        unique_rows._parquet_column_expression(
            column,
            available,
            has_geo_metadata=has_geo_metadata,
            path=Path("region.parquet"),
        )
        == expected
    )


@pytest.mark.parametrize(
    ("column", "expected"),
    [
        (
            "localized_names",
            "CAST([] AS STRUCT(key VARCHAR, value VARCHAR)[]) AS localized_names",
        ),
        (
            "localized_descriptions",
            "CAST([] AS STRUCT(key VARCHAR, value VARCHAR)[]) AS localized_descriptions",
        ),
        ("tags", "CAST([] AS STRUCT(key VARCHAR, value VARCHAR)[]) AS tags"),
    ],
)
def test_missing_mapping_columns_use_typed_empty_lists(column: str, expected: str) -> None:
    assert (
        unique_rows._missing_parquet_column_expression(
            column,
            path=Path("region.parquet"),
            required_columns=frozenset(),
        )
        == expected
    )


def test_missing_requested_mapping_column_is_rejected() -> None:
    """The refusal names the file, which is the only way an operator finds it."""
    with pytest.raises(
        unique_rows.UniqueRowsError,
        match=exactly("missing unique-row column 'tags' in region.parquet"),
    ):
        unique_rows._parquet_column_expression(
            "tags",
            set(),
            has_geo_metadata=False,
            path=Path("region.parquet"),
            required_columns=frozenset({"tags"}),
        )


def test_unique_rows_sql_requires_text_only_when_opted_in() -> None:
    columns = ("osm_type", "osm_id", "description", "localized_descriptions")
    without_text_filter = unique_rows.unique_rows_sql(
        "all_features", columns, key_value_columns_are_maps=True
    )
    with_text_filter = unique_rows.unique_rows_sql(
        "all_features",
        columns,
        key_value_columns_are_maps=True,
        require_successful_text=True,
    )

    assert "WHERE (description IS NOT NULL" not in without_text_filter
    assert "WHERE (description IS NOT NULL" in with_text_filter


def test_parquet_column_expression_rejects_missing_required_column() -> None:
    with pytest.raises(
        unique_rows.UniqueRowsError,
        match=exactly("missing unique-row column 'osm_id' in region.parquet"),
    ):
        unique_rows._parquet_column_expression(
            "osm_id",
            set(),
            has_geo_metadata=False,
            path=Path("region.parquet"),
        )


def test_parquet_select_reads_schema_once_and_builds_pruned_sql(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    schema = SimpleNamespace(
        names=["source_pbf", "osm_type", "osm_id", "geometry"],
        metadata={b"geo": b"{}"},
    )
    parquet_file = SimpleNamespace(schema_arrow=schema)
    monkeypatch.setattr(unique_rows.pq, "ParquetFile", lambda _path: parquet_file)

    sql = unique_rows._parquet_select(
        Path("region's.parquet"),
        ("source_pbf", "osm_type", "osm_id", "version", "geometry"),
    )

    assert "CAST(NULL AS INTEGER) AS version" in sql
    assert "ST_AsWKB(ST_GeomFromWKB(ST_AsWKB(geometry))) AS geometry" in sql
    assert "FROM read_parquet('region''s.parquet')" in sql


def test_iter_unique_parquet_batches_returns_no_rows_for_an_empty_dataset(
    tmp_path: Path,
) -> None:
    assert (
        list(
            unique_rows.iter_unique_parquet_batches(
                tmp_path,
                columns=("osm_type", "osm_id", "geometry"),
            )
        )
        == []
    )


def test_iter_unique_parquet_batches_rejects_non_positive_batch_size(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="batch_size must be positive"):
        list(
            unique_rows.iter_unique_parquet_batches(
                tmp_path,
                columns=("osm_type", "osm_id", "geometry"),
                batch_size=0,
            )
        )


def test_unique_batch_size_accepts_one_and_preserves_exact_error_message() -> None:
    unique_rows._require_batch_size(1)

    with pytest.raises(ValueError) as error:
        unique_rows._require_batch_size(0)

    assert str(error.value) == "batch_size must be positive"


def test_parquet_paths_sort_by_filename_and_use_the_lowercase_data_directory(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    data_root = tmp_path / "dataset"

    def fake_glob(path: Path, pattern: str) -> list[Path]:
        assert path == data_root / "data"
        assert pattern == "*.parquet"
        return [Path("/z/alpha.parquet"), Path("/a/beta.parquet")]

    monkeypatch.setattr(Path, "glob", fake_glob)

    assert unique_rows._parquet_paths(data_root) == (
        Path("/z/alpha.parquet"),
        Path("/a/beta.parquet"),
    )


def test_open_unique_rows_connection_uses_stable_duckdb_configuration(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    calls: list[tuple[object, ...]] = []

    class _Connection:
        def execute(self, *arguments: object) -> None:
            calls.append(arguments)

    connection = _Connection()

    def fake_connect(target: str) -> _Connection:
        calls.append((target,))
        return connection

    monkeypatch.setattr(unique_rows.duckdb, "connect", fake_connect)

    assert unique_rows._open_unique_rows_connection(tmp_path) is connection
    assert calls == [
        (":memory:",),
        ("SET temp_directory = ?", [str(tmp_path / ".work" / "duckdb")]),
    ]


def test_iter_unique_parquet_batches_passes_data_root_to_reader(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    data_root = tmp_path / "dataset"
    data_dir = data_root / "data"
    data_dir.mkdir(parents=True)
    (data_dir / "region.parquet").write_bytes(b"placeholder")
    calls: list[tuple[object, ...]] = []

    class _Connection:
        def close(self) -> None:
            calls.append(("close",))

    connection = _Connection()
    monkeypatch.setattr(unique_rows, "_unique_rows_query", lambda *_args: "query")
    monkeypatch.setattr(unique_rows, "_open_unique_rows_connection", lambda _root: connection)

    def read_rows(
        current_connection: object,
        query: str,
        batch_size: int,
        current_root: Path,
    ) -> object:
        calls.append((current_connection, query, batch_size, current_root))
        return iter(())

    monkeypatch.setattr(unique_rows, "_read_unique_rows", read_rows)

    assert (
        list(
            unique_rows.iter_unique_parquet_batches(
                data_root,
                columns=("osm_type", "osm_id", "geometry"),
                batch_size=1,
            )
        )
        == []
    )
    assert calls == [(connection, "query", 1, data_root), ("close",)]


def test_iter_unique_parquet_batches_validates_before_reading_paths(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    data_root = tmp_path / "dataset"
    (data_root / "data").mkdir(parents=True)
    calls: list[Path] = []
    monkeypatch.setattr(
        "osm_polygon_description_tag.dataset.storage.validate_finalized_artifacts",
        lambda root: calls.append(root),
    )

    assert (
        list(
            unique_rows.iter_unique_parquet_batches(
                data_root,
                columns=("osm_type", "osm_id", "geometry"),
                validate=True,
            )
        )
        == []
    )
    assert calls == [data_root]


def test_iter_unique_parquet_batches_yields_the_selected_payload_row(
    tmp_path: Path,
) -> None:
    first = make_record_dict(
        Polygon([(0, 0), (0, 1), (1, 1), (1, 0)]),
        {"description": "older"},
        osm_id=7,
        source_pbf="a.osm.pbf",
    )
    second = make_record_dict(
        Polygon([(10, 10), (10, 11), (11, 11), (11, 10)]),
        {"description": "newer"},
        osm_id=7,
        source_pbf="b.osm.pbf",
    )
    second["version"] = int(first["version"]) + 1
    data_root = tmp_path / "dataset"
    write_finalized_dataset(
        data_root,
        tmp_path / "raw",
        {"a": [first], "b": [second]},
    )

    rows = [
        row
        for batch in unique_rows.iter_unique_parquet_batches(
            data_root,
            columns=(
                "osm_type",
                "osm_id",
                "description",
                "bbox_min_x",
                "bbox_min_y",
                "bbox_max_x",
                "bbox_max_y",
                "geometry",
            ),
        )
        for row in batch.to_pylist()
    ]

    assert rows == [
        {
            "osm_type": "way",
            "osm_id": 7,
            "description": "newer",
            "bbox_min_x": 10.0,
            "bbox_min_y": 10.0,
            "bbox_max_x": 11.0,
            "bbox_max_y": 11.0,
            "geometry": second["geometry"],
        }
    ]


def test_unique_selection_is_order_independent_for_equal_rank_opposite_endian_rows(
    tmp_path: Path,
) -> None:
    geometry = Polygon([(0, 0), (0, 1), (1, 1), (1, 0)])
    first = make_record_dict(
        geometry,
        {"description": "z payload"},
        osm_id=77,
        source_pbf="overlap.osm.pbf",
    )
    second = make_record_dict(
        geometry,
        {"description": "a payload"},
        osm_id=77,
        source_pbf="overlap.osm.pbf",
    )
    second["geometry"] = to_wkb(geometry, byte_order=0, output_dimension=2)
    second["version"] = first["version"]
    second["timestamp"] = first["timestamp"]

    def selected(name: str, records: list[dict[str, object]]) -> list[dict[str, object]]:
        data_root = tmp_path / name / "generated"
        write_finalized_dataset(
            data_root,
            tmp_path / name / "raw",
            {"region-a": [records[0]], "region-b": [records[1]]},
        )
        return [
            row
            for batch in unique_rows.iter_unique_parquet_batches(
                data_root,
                columns=(
                    "osm_type",
                    "osm_id",
                    "description",
                    "area_m2",
                    "bbox_min_x",
                    "bbox_min_y",
                    "bbox_max_x",
                    "bbox_max_y",
                    "geometry",
                ),
            )
            for row in batch.to_pylist()
        ]

    forward = selected("forward", [first, second])
    reverse = selected("reverse", [second, first])
    expected = select_canonical_row((first, second))

    assert forward == reverse
    assert forward == [
        {
            **{key: expected[key] for key in forward[0] if key != "geometry"},
            "geometry": canonical_geometry_wkb(expected["geometry"]),
        }
    ]


def test_text_filter_runs_before_canonical_ranking(tmp_path: Path) -> None:
    geometry = Polygon([(0, 0), (0, 1), (1, 1), (1, 0)])
    valid = make_record_dict(
        geometry,
        {"description": "valid lower-ranked text"},
        osm_id=88,
        source_pbf="valid.osm.pbf",
    )
    invalid = dict(valid, description="   ", version=int(valid["version"]) + 1)
    invalid["localized_descriptions"] = {}
    data_root = tmp_path / "dataset"

    with patch(
        "osm_polygon_description_tag.dataset.storage.validate_geoparquet",
        return_value=1,
    ):
        write_finalized_dataset(
            data_root,
            tmp_path / "raw",
            {"a": [invalid], "b": [valid]},
        )

    rows = [
        row
        for batch in unique_rows.iter_unique_parquet_batches(
            data_root,
            columns=("osm_type", "osm_id", "description"),
            require_successful_text=True,
        )
        for row in batch.to_pylist()
    ]

    assert rows == [{"osm_type": "way", "osm_id": 88, "description": "valid lower-ranked text"}]


def test_iter_unique_parquet_batches_wraps_duckdb_errors(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    data_root = tmp_path / "dataset"
    data_dir = data_root / "data"
    data_dir.mkdir(parents=True)
    (data_dir / "region.parquet").write_bytes(b"placeholder")
    monkeypatch.setattr(unique_rows, "_parquet_relation", lambda paths, columns: "relation")
    monkeypatch.setattr(
        unique_rows,
        "unique_rows_sql",
        lambda relation, columns: "SELECT * FROM missing",
    )

    with pytest.raises(unique_rows.UniqueRowsError, match="cannot read unique Parquet rows"):
        list(
            unique_rows.iter_unique_parquet_batches(
                data_root,
                columns=("osm_type", "osm_id", "geometry"),
            )
        )


def test_the_relation_requires_every_column_the_caller_asked_for(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A requested column missing from a file must refuse, not silently NULL.

    ``_parquet_relation`` widens the required set with the caller's columns; if
    that widening is dropped, a file lacking a requested column quietly yields
    ``NULL AS <column>`` and the uniqueness result changes without any error.
    """
    seen: list[dict[str, object]] = []

    def _spy(path: Path, columns: object, **kwargs: object) -> str:
        seen.append(kwargs)
        return "SELECT 1"

    monkeypatch.setattr(unique_rows, "_parquet_select", _spy)

    unique_rows._parquet_relation([Path("a.parquet")], ("description",))

    assert seen == [
        {"required_columns": frozenset({*unique_rows._REQUIRED_COLUMNS, "description"})}
    ]


def test_the_relation_joins_its_selects_with_union_all(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The generated SQL is a deterministic artifact, so its text is pinned."""
    monkeypatch.setattr(unique_rows, "_parquet_select", lambda *_a, **_k: "SELECT 1")

    relation = unique_rows._parquet_relation([Path("a.parquet"), Path("b.parquet")], ())

    assert relation == "SELECT 1 UNION ALL SELECT 1"
