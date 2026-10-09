"""Direct contracts for deduplication staging, promotion, and state."""

import os
from pathlib import Path
from types import SimpleNamespace

import duckdb
import pyarrow.parquet as pq
import pytest
from shapely import from_wkb, to_wkb
from shapely.geometry import Polygon

import osm_polygon_description_tag.dataset.deduplication as dedup_module
import osm_polygon_description_tag.dataset.stats as stats_module
import osm_polygon_description_tag.runtime.atomic as atomic_module
from osm_polygon_description_tag.dataset import canonical_rows
from osm_polygon_description_tag.dataset.deduplication import (
    DUPLICATE_REJECTION_REASON,
    DeduplicationError,
    _assert_known_sources,
    _batches_for_source,
    _canonical_relation,
    _read_manifests,
    _read_state,
    _recorded_input_hashes,
    _sql_literal,
    _stage_source,
    _staged_input_drift_names,
    _validated_parquets,
    _verify_staged_inputs,
    _write_state,
    select_canonical_row,
)
from osm_polygon_description_tag.dataset.manifest import (
    Manifest,
    RunCounts,
    file_sha256,
    output_identity_for,
    read_manifest,
    source_identity_for,
    write_manifest,
)
from osm_polygon_description_tag.dataset.schema import SCHEMA
from osm_polygon_description_tag.dataset.storage import write_geoparquet
from osm_polygon_description_tag.dataset.storage_validation import validate_geoparquet
from tests.conftest import make_record_dict
from tests.helpers.dataset import two_deduplication_records as _two_records


def _manifest_for(
    data_root: Path,
    source_root: Path,
    stem: str,
    rows: int,
    output: Path,
    *,
    rejections: dict[str, int] | None = None,
) -> Manifest:
    source = source_root / f"{stem}.osm.pbf"
    source.write_bytes(stem.encode())
    manifest = Manifest(
        manifest_schema_version=2,
        schema_version=3,
        geoparquet_version="1.1.0",
        transform_algorithm_version=3,
        area_policy_sha256="0" * 64,
        output_algorithm_revision="x" * 64,
        source=source_identity_for(source),
        output=output_identity_for(output),
        osmium_version="osmium version test",
        dependency_versions={},
        code_revision=None,
        started_at="2026-01-01T00:00:00+00:00",
        completed_at="2026-01-01T00:00:00+00:00",
        counts=RunCounts(
            emitted_features=rows,
            included_rows=rows,
            rejections=dict(rejections or {}),
        ),
    )
    write_manifest(manifest, data_root / "manifests" / f"{stem}.manifest.json")
    return manifest


@pytest.mark.parametrize(
    ("rejections", "expected_duplicates"),
    [({}, 1), ({DUPLICATE_REJECTION_REASON: 5}, 6)],
)
def test_stage_source_writes_reduced_parquet_and_manifest(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    rejections: dict[str, int],
    expected_duplicates: int,
) -> None:
    data_root = tmp_path / "generated"
    source_root = tmp_path / "raw"
    (data_root / "data").mkdir(parents=True)
    (data_root / "manifests").mkdir()
    source_root.mkdir()
    parquet = data_root / "data" / "a.parquet"
    records = _two_records()
    assert write_geoparquet(records, parquet) == 2
    manifest = _manifest_for(
        data_root,
        source_root,
        "a",
        2,
        parquet,
        rejections=rejections,
    )
    stage_root = data_root / ".work" / "stage"
    seen: dict[str, object] = {}
    real_writer = dedup_module.write_geoparquet_batches
    real_manifest_writer = dedup_module.write_manifest

    def writer(*args: object, **kwargs: object) -> int:
        seen["target"] = args[1]
        seen["batch_size"] = kwargs.get("batch_size")
        return real_writer(*args, **kwargs)  # type: ignore[arg-type]

    def manifest_writer(actual_manifest: Manifest, target: Path) -> None:
        seen["manifest_target"] = target
        real_manifest_writer(actual_manifest, target)

    monkeypatch.setattr(dedup_module, "write_geoparquet_batches", writer)
    monkeypatch.setattr(dedup_module, "write_manifest", manifest_writer)

    connection = duckdb.connect()
    try:
        connection.execute(
            "CREATE TEMP TABLE deduplicated AS SELECT * FROM read_parquet(?) WHERE osm_id = 1",
            [str(parquet)],
        )
        new_rows, entry = _stage_source(connection, parquet, manifest, stage_root)
    finally:
        connection.close()

    assert new_rows == 1
    assert entry is not None
    assert entry["duplicate_rows"] == 1
    staged_parquet = stage_root / "data" / "a.parquet"
    staged_manifest = stage_root / "manifests" / "a.manifest.json"
    assert staged_parquet.is_file()
    assert staged_manifest.is_file()
    assert seen["target"] == staged_parquet
    assert seen["manifest_target"] == staged_manifest
    assert seen["batch_size"] == dedup_module._BATCH_SIZE
    assert validate_geoparquet(staged_parquet) == 1
    assert entry["parquet_sha256"] == file_sha256(staged_parquet)
    assert entry["manifest_sha256"] == file_sha256(staged_manifest)
    assert set(entry) == {
        "parquet",
        "manifest",
        "parquet_sha256",
        "manifest_sha256",
        "duplicate_rows",
    }
    rewritten = read_manifest(staged_manifest)
    assert rewritten.counts.included_rows == 1
    assert rewritten.counts.rejections == {DUPLICATE_REJECTION_REASON: expected_duplicates}
    assert rewritten.output == output_identity_for(staged_parquet)


def test_stage_source_treats_missing_count_row_as_zero(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    data_root = tmp_path / "generated"
    source_root = tmp_path / "raw"
    (data_root / "data").mkdir(parents=True)
    (data_root / "manifests").mkdir()
    source_root.mkdir()
    parquet = data_root / "data" / "a.parquet"
    write_geoparquet(_two_records(), parquet)
    manifest = _manifest_for(data_root, source_root, "a", 2, parquet)
    stage_root = data_root / ".work" / "stage"

    class Result:
        def fetchone(self) -> None:
            return None

        def to_arrow_reader(self, _batch_size: int) -> tuple[object, ...]:
            return ()

    class Connection:
        def execute(self, _query: str, _parameters: list[object]) -> Result:
            return Result()

    def fake_writer(records: object, target: Path, **_kwargs: object) -> int:
        assert list(records) == []
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(b"staged")
        return 0

    monkeypatch.setattr(dedup_module, "write_geoparquet_batches", fake_writer)

    new_rows, entry = _stage_source(Connection(), parquet, manifest, stage_root)

    assert new_rows == 0
    assert entry is not None
    assert entry["duplicate_rows"] == 2


def test_stage_source_returns_no_stage_when_no_rows_are_dropped(tmp_path: Path) -> None:
    data_root = tmp_path / "generated"
    source_root = tmp_path / "raw"
    (data_root / "data").mkdir(parents=True)
    (data_root / "manifests").mkdir()
    source_root.mkdir()
    parquet = data_root / "data" / "a.parquet"
    records = _two_records()
    write_geoparquet(records, parquet)
    manifest = _manifest_for(data_root, source_root, "a", 2, parquet)
    stage_root = data_root / ".work" / "stage"

    connection = duckdb.connect()
    try:
        connection.execute(
            "CREATE TEMP TABLE deduplicated AS SELECT * FROM read_parquet(?)",
            [str(parquet)],
        )
        result = _stage_source(connection, parquet, manifest, stage_root)
    finally:
        connection.close()

    assert result == (2, None)
    assert not stage_root.exists()


def test_write_state_is_sorted_atomic_json_with_trailing_newline(tmp_path: Path) -> None:
    path = tmp_path / ".work" / "dedup-state.json"
    _write_state(path, {"z": 1, "a": {"b": 2}})

    assert path.read_text(encoding="utf-8") == ('{\n  "a": {\n    "b": 2\n  },\n  "z": 1\n}\n')
    assert list(path.parent.glob("*.tmp")) == []


def test_write_state_preserves_unicode_json_bytes(tmp_path: Path) -> None:
    path = tmp_path / ".work" / "state.json"

    _write_state(path, {"value": "café"})

    assert '"café"'.encode() in path.read_bytes()


def test_write_state_writes_sorted_indented_unicode_json_bytes(tmp_path: Path) -> None:
    path = tmp_path / "state.json"

    _write_state(path, {"z": "é", "a": 1})

    assert path.read_bytes() == '{\n  "a": 1,\n  "z": "é"\n}\n'.encode()


def test_write_state_keeps_unicode_unescaped(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    options: dict[str, object] = {}
    original_dumps = dedup_module.json.dumps

    def dumps(value: object, *args: object, **kwargs: object) -> str:
        options.update(kwargs)
        return original_dumps(value, *args, **kwargs)  # type: ignore[arg-type]

    monkeypatch.setattr(dedup_module.json, "dumps", dumps)

    _write_state(tmp_path / "state.json", {"value": "café"})

    assert options["ensure_ascii"] is False


def test_write_state_creates_nested_parent_and_atomically_replaces_existing_state(
    tmp_path: Path,
) -> None:
    path = tmp_path / ".work" / "nested" / "dedup-state.json"

    _write_state(path, {"value": "first"})
    _write_state(path, {"value": "second"})

    assert _read_state(path) == {"value": "second"}
    assert list(path.parent.glob("*.tmp")) == []


def test_write_state_fsyncs_the_file_and_parent_directory(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    opened_directories: list[tuple[str, int]] = []
    synced_descriptors: list[int] = []
    closed_descriptors: list[int] = []
    directory_fd = 321

    def open_directory(path: str, flags: int, **_kwargs: object) -> int:
        opened_directories.append((path, flags))
        return directory_fd

    monkeypatch.setattr(
        atomic_module.os,
        "open",
        open_directory,
    )
    monkeypatch.setattr(
        atomic_module.os, "fsync", lambda descriptor: synced_descriptors.append(descriptor)
    )
    monkeypatch.setattr(
        atomic_module.os, "close", lambda descriptor: closed_descriptors.append(descriptor)
    )
    path = tmp_path / ".work" / "state.json"

    _write_state(path, {"value": "durable"})

    assert opened_directories == [(str(path.parent), os.O_RDONLY)]
    assert synced_descriptors[-1] == directory_fd
    assert closed_descriptors == [directory_fd]


@pytest.mark.parametrize(
    ("value", "expected"),
    [("plain", "'plain'"), ("O'Reilly", "'O''Reilly'"), ("", "''")],
)
def test_sql_literal_escapes_values_for_sql_string_literals(value: str, expected: str) -> None:
    assert _sql_literal(value) == expected


def test_read_state_returns_none_when_state_file_is_absent(tmp_path: Path) -> None:
    assert _read_state(tmp_path / "missing.json") is None


def test_read_state_requests_utf8_decoding(monkeypatch: pytest.MonkeyPatch) -> None:
    encodings: list[str | None] = []

    class StatePath:
        def is_file(self) -> bool:
            return True

        def read_text(self, *, encoding: str | None = None) -> str:
            encodings.append(encoding)
            return '{"value": "café"}'

        def __str__(self) -> str:
            return "state.json"

    state_path = StatePath()

    assert _read_state(state_path) == {"value": "café"}  # type: ignore[arg-type]
    assert encodings == ["utf-8"]


@pytest.mark.parametrize("contents", ["not json", "[]"])
def test_read_state_rejects_invalid_or_non_object_payloads(tmp_path: Path, contents: str) -> None:
    path = tmp_path / "state.json"
    path.write_text(contents, encoding="utf-8")

    with pytest.raises(DeduplicationError, match="deduplication state"):
        _read_state(path)


def test_recorded_input_hashes_requires_a_mapping_with_a_stable_error() -> None:
    with pytest.raises(DeduplicationError) as error:
        _recorded_input_hashes({})

    assert str(error.value) == "staged deduplication state is missing input identities"


def test_staged_input_drift_names_reports_missing_and_extra_inputs() -> None:
    assert _staged_input_drift_names(
        {"a.parquet": "a"},
        {"a.parquet": "a", "b.parquet": "b"},
        {},
    ) == ("b.parquet",)
    assert _staged_input_drift_names(
        {"a.parquet": "a", "b.parquet": "b"},
        {"a.parquet": "a"},
        {},
    ) == ("b.parquet",)
    assert (
        _staged_input_drift_names(
            {"a.parquet": "new"},
            {"a.parquet": "old"},
            {"a.parquet": "new"},
        )
        == ()
    )
    assert _staged_input_drift_names(
        {"a.parquet": "new"},
        {"a.parquet": "old"},
        {},
    ) == ("a.parquet",)


def test_verify_staged_inputs_reports_all_drifted_names_in_order(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    (tmp_path / "data").mkdir()
    monkeypatch.setattr(
        dedup_module,
        "_input_hashes",
        lambda _paths: {"b.parquet": "new-b", "a.parquet": "new-a"},
    )

    with pytest.raises(DeduplicationError) as error:
        _verify_staged_inputs(
            tmp_path,
            {
                "inputs": {"a.parquet": "old-a", "b.parquet": "old-b"},
                "files": [],
            },
        )

    assert str(error.value) == (
        "staged deduplication inputs changed; refusing to resume: a.parquet, b.parquet"
    )


def test_validated_parquets_returns_empty_for_missing_or_empty_data_directory(
    tmp_path: Path,
) -> None:
    assert _validated_parquets(tmp_path / "missing") == ()

    data_root = tmp_path / "generated"
    (data_root / "data").mkdir(parents=True)
    assert _validated_parquets(data_root) == ()


def test_validated_parquets_validates_each_discovered_parquet(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    data_root = tmp_path / "generated"
    parquet = data_root / "data" / "a.parquet"
    parquet.parent.mkdir(parents=True)
    parquet.write_bytes(b"placeholder")
    validated_calls: list[Path] = []
    geoparquet_calls: list[Path] = []
    monkeypatch.setattr(
        dedup_module,
        "validate_finalized_artifacts",
        lambda root: validated_calls.append(root) or {"parquets": [parquet]},
    )
    monkeypatch.setattr(
        dedup_module,
        "validate_geoparquet",
        lambda path: geoparquet_calls.append(path) or 1,
    )

    assert _validated_parquets(data_root) == (parquet,)
    assert validated_calls == [data_root]
    assert geoparquet_calls == [parquet]


def test_validated_parquets_uses_the_lowercase_data_directory_name(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    requested_parts: list[str] = []
    parquet = Path("generated/data/a.parquet")

    class DataDirectory:
        def is_dir(self) -> bool:
            return True

        def glob(self, _pattern: str) -> tuple[Path, ...]:
            return (parquet,)

    class DataRoot:
        def __truediv__(self, part: str) -> DataDirectory:
            requested_parts.append(part)
            return DataDirectory()

    monkeypatch.setattr(
        dedup_module,
        "validate_finalized_artifacts",
        lambda _root: {"parquets": [parquet]},
    )
    monkeypatch.setattr(dedup_module, "validate_geoparquet", lambda _path: 1)

    assert _validated_parquets(DataRoot()) == (parquet,)  # type: ignore[arg-type]
    assert requested_parts == ["data"]


def test_assert_known_sources_accepts_rows_backed_by_manifests() -> None:
    connection = duckdb.connect()
    try:
        connection.execute("CREATE TEMP TABLE deduplicated(source_pbf VARCHAR)")
        connection.execute("INSERT INTO deduplicated VALUES ('a.osm.pbf')")
        manifests = {"a.parquet": SimpleNamespace(source=SimpleNamespace(name="a.osm.pbf"))}

        _assert_known_sources(connection, manifests)
    finally:
        connection.close()


def test_assert_known_sources_rejects_rows_without_a_manifest() -> None:
    connection = duckdb.connect()
    try:
        connection.execute("CREATE TEMP TABLE deduplicated(source_pbf VARCHAR)")
        connection.execute("INSERT INTO deduplicated VALUES ('known.osm.pbf'), ('unknown.osm.pbf')")
        manifests = {"known.parquet": SimpleNamespace(source=SimpleNamespace(name="known.osm.pbf"))}

        with pytest.raises(
            DeduplicationError,
            match=r"rows reference unknown source PBFs: \['unknown\.osm\.pbf'\]",
        ):
            _assert_known_sources(connection, manifests)
    finally:
        connection.close()


def test_assert_known_sources_queries_the_deduplicated_relation() -> None:
    queries: list[str] = []

    class Result:
        def fetchall(self) -> list[tuple[str]]:
            return [("a.osm.pbf",)]

    class Connection:
        def execute(self, query: str) -> Result:
            queries.append(query)
            return Result()

    manifests = {"a.parquet": SimpleNamespace(source=SimpleNamespace(name="a.osm.pbf"))}

    _assert_known_sources(Connection(), manifests)  # type: ignore[arg-type]

    assert queries == ["SELECT DISTINCT source_pbf FROM deduplicated"]


def test_batches_for_source_filters_and_orders_canonical_rows(tmp_path: Path) -> None:
    parquet = tmp_path / "rows.parquet"
    records = _two_records()
    records.reverse()
    other = tmp_path / "other.parquet"
    other_record = dict(records[0], osm_id=99, source_pbf="other.osm.pbf")
    write_geoparquet(records, parquet)
    write_geoparquet([other_record], other)

    connection = duckdb.connect()
    try:
        connection.execute(
            "CREATE TEMP TABLE deduplicated AS SELECT * FROM read_parquet(?)",
            [str(parquet)],
        )
        connection.execute(
            "INSERT INTO deduplicated SELECT * FROM read_parquet(?)",
            [str(other)],
        )
        batches = _batches_for_source(connection, "a.osm.pbf")
        rows = [row for batch in batches for row in batch.to_pylist()]
    finally:
        connection.close()

    assert [row["osm_id"] for row in rows] == [1, 2]
    assert {row["source_pbf"] for row in rows} == {"a.osm.pbf"}


def test_batches_for_source_builds_the_expected_filter_query() -> None:
    queries: list[tuple[str, list[str]]] = []

    class Result:
        def to_arrow_reader(self, batch_size: int) -> tuple[object, ...]:
            assert batch_size == dedup_module._BATCH_SIZE
            return ()

    class Connection:
        def execute(self, query: str, parameters: list[str]) -> Result:
            queries.append((query, parameters))
            return Result()

    assert tuple(_batches_for_source(Connection(), "a.osm.pbf")) == ()  # type: ignore[arg-type]
    assert queries == [
        (
            f"SELECT {', '.join(SCHEMA.names)} FROM deduplicated "  # noqa: S608
            "WHERE source_pbf = ? ORDER BY osm_type, osm_id",
            ["a.osm.pbf"],
        )
    ]


def test_canonical_relation_keeps_one_highest_version_per_osm_identity(
    tmp_path: Path,
) -> None:
    duplicate = make_record_dict(
        Polygon([(0, 0), (0, 1), (1, 1), (1, 0)]),
        {"description": "duplicate"},
        osm_id=1,
        source_pbf="z.osm.pbf",
    )
    older = dict(duplicate, version=1)
    newer = dict(
        duplicate,
        version=2,
        source_pbf="a.osm.pbf",
    )
    unique = make_record_dict(
        Polygon([(2, 2), (2, 3), (3, 3), (3, 2)]),
        {"description": "unique"},
        osm_id=2,
        source_pbf="b.osm.pbf",
    )
    first = tmp_path / "first.parquet"
    second = tmp_path / "second.parquet"
    third = tmp_path / "third.parquet"
    write_geoparquet([older], first)
    write_geoparquet([newer], second)
    write_geoparquet([unique], third)

    connection = duckdb.connect()
    try:
        _canonical_relation(connection, (first, second, third))
        rows = connection.execute(
            "SELECT osm_id, version, source_pbf FROM deduplicated ORDER BY osm_id"
        ).fetchall()
    finally:
        connection.close()

    assert rows == [(1, 2, "a.osm.pbf"), (2, 1, "b.osm.pbf")]


def test_canonical_relation_selects_the_same_differing_payload_row_in_any_order(
    tmp_path: Path,
) -> None:
    base = make_record_dict(
        Polygon([(0, 0), (0, 1), (1, 1), (1, 0)]),
        {"description": "same"},
        osm_id=77,
        source_pbf="same.osm.pbf",
    )
    candidate = dict(
        base,
        area_m2=999.0,
        bbox_min_x=10.0,
        bbox_min_y=10.0,
        bbox_max_x=11.0,
        bbox_max_y=11.0,
    )
    first = tmp_path / "first.parquet"
    second = tmp_path / "second.parquet"
    write_geoparquet([base], first)
    write_geoparquet([candidate], second)

    def selected(paths: tuple[Path, Path]) -> tuple[object, ...]:
        connection = duckdb.connect()
        try:
            _canonical_relation(connection, paths)
            return connection.execute(
                """
                SELECT area_m2, bbox_min_x, bbox_min_y, bbox_max_x, bbox_max_y
                FROM deduplicated
                WHERE osm_id = 77
                """
            ).fetchone()
        finally:
            connection.close()

    expected = (999.0, 10.0, 10.0, 11.0, 11.0)
    assert selected((first, second)) == expected
    assert selected((second, first)) == expected


def test_all_canonical_selectors_choose_the_same_payload_for_opposite_endian_geometry(
    tmp_path: Path,
) -> None:
    geometry = Polygon([(0, 0), (0, 1), (1, 1), (1, 0)])
    base = make_record_dict(
        geometry,
        {"description": "base"},
        osm_id=77,
        source_pbf="same.osm.pbf",
    )
    little_endian = dict(
        base,
        area_m2=1.0,
        description="a-1",
        geometry=to_wkb(geometry, byte_order=1),
        timestamp=None,
    )
    big_endian = dict(
        base,
        area_m2=5.0,
        description="b-5",
        geometry=to_wkb(geometry, byte_order=0),
        timestamp=None,
    )
    expected = select_canonical_row((little_endian, big_endian))
    assert expected["description"] == "a-1"
    expected_raw_geometry = expected["geometry"]
    assert isinstance(expected_raw_geometry, bytes | bytearray | memoryview)
    expected_geometry = to_wkb(from_wkb(bytes(expected_raw_geometry)), byte_order=1)
    expected_selection = (
        expected["description"],
        expected["area_m2"],
        expected_geometry,
        canonical_rows._row_fingerprint(expected),
    )

    first = tmp_path / "first.parquet"
    second = tmp_path / "second.parquet"
    write_geoparquet([little_endian], first)
    write_geoparquet([big_endian], second)

    def selected_parquet(paths: tuple[Path, Path]) -> tuple[object, ...]:
        connection = duckdb.connect()
        try:
            _canonical_relation(connection, paths)
            row = connection.execute(
                "SELECT description, area_m2, geometry, "  # noqa: S608 - shared canonical SQL policy
                f"{canonical_rows._full_row_fingerprint_sql()} "
                "FROM deduplicated WHERE osm_id = 77"
            ).fetchone()
        finally:
            connection.close()
        assert row is not None
        return row[0], row[1], bytes(row[2]), row[3]

    def selected_stats(paths: tuple[Path, Path]) -> tuple[object, ...]:
        connection = duckdb.connect()
        try:
            stats_module._create_feature_table(connection)
            for path in paths:
                reader = pq.ParquetFile(path)
                for batch in reader.iter_batches(columns=stats_module._FEATURE_COLUMNS):
                    stats_module._insert_batch(connection, batch, path.name)
            stats_module._create_unique_feature_view(connection)
            row = connection.execute(
                "SELECT description, area_m2, geometry, "  # noqa: S608 - shared canonical SQL policy
                f"{canonical_rows._full_row_fingerprint_sql(key_value_columns_are_maps=True)} "
                "FROM features WHERE osm_id = 77"
            ).fetchone()
        finally:
            connection.close()
        assert row is not None
        return row[0], row[1], bytes(row[2]), row[3]

    for paths in ((first, second), (second, first)):
        assert (
            select_canonical_row(
                (little_endian, big_endian)
                if paths == (first, second)
                else (big_endian, little_endian)
            )["description"]
            == expected["description"]
        )
        assert selected_parquet(paths) == expected_selection
        assert selected_stats(paths) == expected_selection


def test_python_and_sql_selectors_choose_the_same_persisted_payload(
    tmp_path: Path,
) -> None:
    first_row = make_record_dict(
        Polygon([(0, 0), (0, 1), (1, 1), (1, 0)]),
        {"description": "same"},
        osm_id=77,
        source_pbf="same.osm.pbf",
    )
    second_row = dict(first_row)
    first_row["area_m2"] = 1.0
    second_row["area_m2"] = 6.0
    first_row["timestamp"] = None
    second_row["timestamp"] = None
    expected = select_canonical_row((first_row, second_row))
    assert expected["area_m2"] == 6.0

    first = tmp_path / "first.parquet"
    second = tmp_path / "second.parquet"
    write_geoparquet([first_row], first)
    write_geoparquet([second_row], second)

    connection = duckdb.connect()
    try:
        _canonical_relation(connection, (first, second))
        actual = connection.execute("SELECT area_m2 FROM deduplicated WHERE osm_id = 77").fetchone()
        sql_fingerprint = connection.execute(
            "SELECT area_m2, "  # noqa: S608 - shared SQL policy
            f"{canonical_rows._full_row_fingerprint_sql()} "
            "FROM deduplicated ORDER BY area_m2"
        ).fetchall()
    finally:
        connection.close()

    assert actual == (expected["area_m2"],)
    assert sql_fingerprint == [
        (expected["area_m2"], canonical_rows._row_fingerprint(expected)),
    ]


def test_read_manifests_maps_each_parquet_to_its_manifest(
    tmp_path: Path,
) -> None:
    data_root = tmp_path / "generated"
    (data_root / "data").mkdir(parents=True)
    (data_root / "manifests").mkdir()
    source_root = tmp_path / "raw"
    source_root.mkdir()
    first = data_root / "data" / "a.parquet"
    second = data_root / "data" / "b.parquet"
    first.write_bytes(b"a")
    second.write_bytes(b"b")
    _manifest_for(data_root, source_root, "a", 1, first)
    _manifest_for(data_root, source_root, "b", 2, second)

    manifests = _read_manifests(data_root, (first, second))

    assert set(manifests) == {"a.parquet", "b.parquet"}
    assert manifests["a.parquet"].source.name == "a.osm.pbf"
    assert manifests["b.parquet"].counts.included_rows == 2
