"""Direct contracts for deduplication staging, promotion, and state."""

import builtins
import hashlib
import os
from collections.abc import Mapping
from pathlib import Path
from types import SimpleNamespace

import duckdb
import pytest
from shapely.geometry import Polygon

import osm_polygon_description_tag.dataset.deduplication as dedup_module
from osm_polygon_description_tag.dataset.deduplication import (
    _STATE_RELATIVE_PATH,
    DEDUPLICATION_POLICY_SHA256,
    DUPLICATE_REJECTION_REASON,
    DeduplicationError,
    DeduplicationResult,
    _assert_known_sources,
    _canonical_relation,
    _complete_result,
    _DeduplicationContext,
    _finish_deduplication,
    _prepare_context,
    _promote_artifact,
    _promote_entry,
    _promote_staged,
    _read_manifests,
    _read_state,
    _resume_staged,
    _rows_for_source,
    _skipped_result,
    _sql_literal,
    _stage_changes,
    _stage_source,
    _state_payload,
    _validated_parquets,
    _write_state,
    deduplicate_dataset,
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
from osm_polygon_description_tag.dataset.storage import validate_geoparquet, write_geoparquet
from tests.conftest import make_record_dict


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


def _two_records() -> list[dict[str, object]]:
    return [
        make_record_dict(
            Polygon([(0, 0), (0, 1), (1, 1), (1, 0)]),
            {"description": "one"},
            osm_id=1,
            source_pbf="a.osm.pbf",
        ),
        make_record_dict(
            Polygon([(2, 2), (2, 3), (3, 3), (3, 2)]),
            {"description": "two"},
            osm_id=2,
            source_pbf="a.osm.pbf",
        ),
    ]


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
    real_writer = dedup_module.write_geoparquet
    real_manifest_writer = dedup_module.write_manifest

    def writer(*args: object, **kwargs: object) -> int:
        seen["target"] = args[1]
        seen["batch_size"] = kwargs.get("batch_size")
        return real_writer(*args, **kwargs)  # type: ignore[arg-type]

    def manifest_writer(actual_manifest: Manifest, target: Path) -> None:
        seen["manifest_target"] = target
        real_manifest_writer(actual_manifest, target)

    monkeypatch.setattr(dedup_module, "write_geoparquet", writer)
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

    monkeypatch.setattr(dedup_module, "write_geoparquet", fake_writer)

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


def test_write_state_uses_utf8_text_and_binary_fsync_reads(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    encodings: list[str | None] = []
    modes: list[str | None] = []
    json_options: dict[str, object] = {}
    original_write_text = Path.write_text
    original_open = builtins.open
    original_dumps = dedup_module.json.dumps

    def write_text(path: Path, data: str, *args: object, **kwargs: object) -> int:
        encodings.append(kwargs.get("encoding"))  # type: ignore[arg-type]
        return original_write_text(path, data, *args, **kwargs)  # type: ignore[arg-type]

    def open_file(file: object, *args: object, **kwargs: object) -> object:
        modes.append(args[0] if args else kwargs.get("mode"))  # type: ignore[arg-type]
        return original_open(file, *args, **kwargs)  # type: ignore[arg-type]

    def dumps(value: object, *args: object, **kwargs: object) -> str:
        json_options.update(kwargs)
        return original_dumps(value, *args, **kwargs)  # type: ignore[arg-type]

    monkeypatch.setattr(Path, "write_text", write_text)
    monkeypatch.setattr(dedup_module, "open", open_file, raising=False)
    monkeypatch.setattr(dedup_module.json, "dumps", dumps)

    _write_state(tmp_path / "state.json", {"value": "durable"})

    assert encodings == ["utf-8"]
    assert modes == ["rb"]
    assert json_options["ensure_ascii"] is False


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
    monkeypatch.setattr(
        dedup_module.os,
        "open",
        lambda path, flags: (opened_directories.append((path, flags)) or directory_fd),
    )
    monkeypatch.setattr(
        dedup_module.os, "fsync", lambda descriptor: synced_descriptors.append(descriptor)
    )
    monkeypatch.setattr(
        dedup_module.os, "close", lambda descriptor: closed_descriptors.append(descriptor)
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
        lambda root: (validated_calls.append(root) or {"parquets": [parquet]}),
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


def test_rows_for_source_filters_and_orders_canonical_rows(tmp_path: Path) -> None:
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
        rows = tuple(_rows_for_source(connection, "a.osm.pbf"))
    finally:
        connection.close()

    assert [row["osm_id"] for row in rows] == [1, 2]
    assert {row["source_pbf"] for row in rows} == {"a.osm.pbf"}


def test_rows_for_source_builds_the_expected_filter_query() -> None:
    queries: list[tuple[str, list[str]]] = []

    class Result:
        def to_arrow_reader(self, batch_size: int) -> tuple[object, ...]:
            assert batch_size == dedup_module._BATCH_SIZE
            return ()

    class Connection:
        def execute(self, query: str, parameters: list[str]) -> Result:
            queries.append((query, parameters))
            return Result()

    assert tuple(_rows_for_source(Connection(), "a.osm.pbf")) == ()  # type: ignore[arg-type]
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


def test_read_manifests_uses_the_lowercase_manifest_directory_name(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    parquet = tmp_path / "data" / "a.parquet"
    seen: list[Path] = []
    sentinel = object()
    monkeypatch.setattr(
        dedup_module,
        "read_manifest",
        lambda path: seen.append(path) or sentinel,
    )

    result = _read_manifests(tmp_path, (parquet,))

    assert result == {"a.parquet": sentinel}
    assert seen == [tmp_path / "manifests" / "a.manifest.json"]


def test_resume_staged_promotes_state_and_returns_deduplicated_result(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    data_root = tmp_path / "generated"
    data_dir = data_root / "data"
    data_dir.mkdir(parents=True)
    output = data_dir / "a.parquet"
    output.write_bytes(b"output")
    state_path = data_root / ".work" / "dedup-state.json"
    state = {
        "status": "staged",
        "stage_dir": ".work/dedup/token",
        "input_rows": 8,
        "output_rows": 6,
        "duplicate_rows": 2,
        "files": [{"parquet": "data/a.parquet"}],
    }
    calls: list[tuple[Path, Mapping[str, object], object]] = []
    writes: list[tuple[Path, dict[str, object]]] = []

    def promote(
        root: Path,
        actual_state: Mapping[str, object],
        *,
        promotion_hook: object = None,
    ) -> None:
        calls.append((root, actual_state, promotion_hook))

    monkeypatch.setattr(dedup_module, "_promote_staged", promote)
    monkeypatch.setattr(
        dedup_module,
        "_write_state",
        lambda path, payload: writes.append((path, dict(payload))),
    )
    hashed_paths: list[Path] = []
    monkeypatch.setattr(
        dedup_module,
        "_input_hashes",
        lambda paths: (hashed_paths.extend(paths) or {"a.parquet": "output-sha"}),
    )
    hook = object()

    result = _resume_staged(data_root, state_path, state, promotion_hook=hook)  # type: ignore[arg-type]

    assert calls == [(data_root, state, hook)]
    assert writes[0][0] == state_path
    assert writes[0][1]["status"] == "complete"
    assert "stage_dir" not in writes[0][1]
    assert writes[0][1]["outputs"] == {"a.parquet": "output-sha"}
    assert hashed_paths == [output]
    assert result == DeduplicationResult("deduplicated", 8, 6, 2, 1)


def test_resume_staged_accepts_state_without_a_stage_directory(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    data_root = tmp_path / "generated"
    output = data_root / "data" / "a.parquet"
    output.parent.mkdir(parents=True)
    output.write_bytes(b"output")
    state = {
        "input_rows": 2,
        "output_rows": 1,
        "duplicate_rows": 1,
        "files": [{"parquet": "data/a.parquet"}],
    }
    writes: list[dict[str, object]] = []
    monkeypatch.setattr(dedup_module, "_promote_staged", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(
        dedup_module,
        "_write_state",
        lambda _path, payload: writes.append(dict(payload)),
    )

    result = _resume_staged(data_root, tmp_path / "state.json", state)

    assert "stage_dir" not in writes[0]
    assert result == DeduplicationResult("deduplicated", 2, 1, 1, 1)


def test_deduplicate_dataset_forwards_promotion_hook_when_resuming_staged(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    state = {"status": "staged"}
    result = DeduplicationResult("deduplicated", 2, 1, 1, 1)
    calls: list[tuple[Path, Path, Mapping[str, object], object]] = []
    hook = object()

    def read_state(path: Path) -> dict[str, object]:
        assert path == tmp_path / _STATE_RELATIVE_PATH
        return state

    def resume(
        root: Path,
        state_path: Path,
        actual_state: Mapping[str, object],
        *,
        promotion_hook: object = None,
    ) -> DeduplicationResult:
        calls.append((root, state_path, actual_state, promotion_hook))
        return result

    monkeypatch.setattr(dedup_module, "_read_state", read_state)
    monkeypatch.setattr(dedup_module, "_resume_staged", resume)

    assert deduplicate_dataset(tmp_path, promotion_hook=hook) is result
    assert calls == [(tmp_path, tmp_path / _STATE_RELATIVE_PATH, state, hook)]


def test_deduplicate_dataset_preserves_state_and_uses_stable_stage_path(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    state = {"status": "pending"}
    context = object()
    result = object()
    seen: dict[str, object] = {}

    monkeypatch.setattr(dedup_module, "_read_state", lambda _path: state)

    def prepare(
        root: Path, state_path: Path, actual_state: Mapping[str, object]
    ) -> tuple[object, None]:
        seen["prepare"] = (root, state_path, actual_state)
        return context, None

    monkeypatch.setattr(dedup_module, "_prepare_context", prepare)
    monkeypatch.setattr(
        dedup_module,
        "uuid",
        SimpleNamespace(uuid4=lambda: SimpleNamespace(hex="fixed-token")),
    )
    monkeypatch.setattr(
        dedup_module,
        "_stage_changes",
        lambda actual_context, stage_root: (
            seen.update(stage=(actual_context, stage_root)) or ([], 4)
        ),
    )
    monkeypatch.setattr(
        dedup_module,
        "_state_payload",
        lambda actual_context, changed, output_rows: (
            seen.update(payload=(actual_context, changed, output_rows)) or {"status": "complete"}
        ),
    )
    monkeypatch.setattr(
        dedup_module,
        "_finish_deduplication",
        lambda actual_context, stage_dir, payload, changed, output_rows, promotion_hook: (
            seen.update(
                finish=(
                    actual_context,
                    stage_dir,
                    payload,
                    changed,
                    output_rows,
                    promotion_hook,
                )
            )
            or result
        ),
    )

    assert deduplicate_dataset(tmp_path) is result
    assert seen["prepare"] == (tmp_path, tmp_path / _STATE_RELATIVE_PATH, state)
    assert seen["stage"] == (context, tmp_path / ".work" / "dedup" / "fixed-token")
    assert seen["payload"] == (context, [], 4)
    assert seen["finish"] == (
        context,
        Path(".work") / "dedup" / "fixed-token",
        {"status": "complete"},
        [],
        4,
        None,
    )


def test_deduplicate_dataset_rejects_missing_context(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setattr(dedup_module, "_read_state", lambda _path: None)
    monkeypatch.setattr(dedup_module, "_prepare_context", lambda *_args: (None, None))

    with pytest.raises(
        DeduplicationError,
        match=r"^deduplication context was not created$",
    ):
        deduplicate_dataset(tmp_path)


def test_prepare_context_returns_cached_result_without_reading_manifests(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    parquet = tmp_path / "data" / "a.parquet"
    state = {"status": "complete"}
    cached = DeduplicationResult("skipped", 1, 1, 0, 0)
    monkeypatch.setattr(dedup_module, "_validated_parquets", lambda _root: (parquet,))
    monkeypatch.setattr(dedup_module, "_input_hashes", lambda _paths: {"a.parquet": "sha"})

    def complete_result(
        actual_state: object, inputs: object, parquets: object
    ) -> DeduplicationResult:
        assert actual_state is state
        assert inputs == {"a.parquet": "sha"}
        assert parquets == (parquet,)
        return cached

    monkeypatch.setattr(dedup_module, "_complete_result", complete_result)
    monkeypatch.setattr(
        dedup_module,
        "_read_manifests",
        lambda *_args: pytest.fail("manifests must not be read for a cached result"),
    )

    context, result = _prepare_context(tmp_path, tmp_path / "state.json", state)

    assert context is None
    assert result is cached


def test_prepare_context_preserves_incomplete_state_in_context(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    parquet = tmp_path / "data" / "a.parquet"
    state = {"status": "staged"}
    manifests = {"a.parquet": SimpleNamespace(source=SimpleNamespace(name="a.osm.pbf"))}
    monkeypatch.setattr(dedup_module, "_validated_parquets", lambda _root: (parquet,))
    monkeypatch.setattr(dedup_module, "_input_hashes", lambda _paths: {"a.parquet": "sha"})
    monkeypatch.setattr(dedup_module, "_complete_result", lambda *_args: None)
    monkeypatch.setattr(dedup_module, "_read_manifests", lambda *_args: manifests)
    monkeypatch.setattr(dedup_module, "_current_output_rows", lambda _paths: 4)

    context, result = _prepare_context(tmp_path, tmp_path / "state.json", state)

    assert result is None
    assert context is not None
    assert context.state is state
    assert context.input_rows == 4
    assert context.manifests == manifests


def test_stage_changes_accumulates_rows_across_all_parquets(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    first = tmp_path / "a.parquet"
    second = tmp_path / "b.parquet"
    context = _DeduplicationContext(
        data_root=tmp_path,
        state_path=tmp_path / "state.json",
        state=None,
        parquets=(first, second),
        manifests={
            "a.parquet": SimpleNamespace(source=SimpleNamespace(name="a.osm.pbf")),
            "b.parquet": SimpleNamespace(source=SimpleNamespace(name="b.osm.pbf")),
        },
        inputs={},
        input_rows=8,
    )
    calls: list[str] = []

    class Connection:
        def close(self) -> None:
            calls.append("close")

    monkeypatch.setattr(dedup_module.duckdb, "connect", lambda: Connection())
    monkeypatch.setattr(dedup_module, "_canonical_relation", lambda *_args: None)
    monkeypatch.setattr(dedup_module, "_assert_known_sources", lambda *_args: None)

    def stage(
        _connection: object, parquet: Path, _manifest: object, _root: Path
    ) -> tuple[int, dict[str, object] | None]:
        calls.append(parquet.name)
        return (3, {"parquet": "data/a.parquet"}) if parquet == first else (5, None)

    monkeypatch.setattr(dedup_module, "_stage_source", stage)

    changed, output_rows = _stage_changes(context, tmp_path / "stage")

    assert changed == [{"parquet": "data/a.parquet"}]
    assert output_rows == 8
    assert calls == ["a.parquet", "b.parquet", "close"]


def test_complete_result_falls_back_to_current_output_rows_when_input_rows_missing(
    tmp_path: Path,
) -> None:
    parquet = tmp_path / "a.parquet"
    write_geoparquet(_two_records(), parquet)
    inputs = {"a.parquet": file_sha256(parquet)}
    state = {
        "status": "complete",
        "policy_sha256": DEDUPLICATION_POLICY_SHA256,
        "outputs": inputs,
    }

    assert _complete_result(state, inputs, (parquet,)) == _skipped_result(2, 2, 0)


def test_complete_result_returns_none_without_state(tmp_path: Path) -> None:
    assert _complete_result(None, {}, ()) is None


@pytest.mark.parametrize(
    ("input_rows", "output_rows", "duplicate_rows"),
    [(0, 0, 0), (8, 6, 2)],
)
def test_skipped_result_builds_machine_readable_result(
    input_rows: int, output_rows: int, duplicate_rows: int
) -> None:
    assert _skipped_result(input_rows, output_rows, duplicate_rows) == DeduplicationResult(
        "skipped", input_rows, output_rows, duplicate_rows, 0
    )


def test_skipped_result_defaults_all_row_counts_to_zero() -> None:
    assert _skipped_result() == DeduplicationResult("skipped", 0, 0, 0, 0)


def test_select_canonical_row_rejects_empty_groups() -> None:
    with pytest.raises(
        ValueError,
        match=r"^cannot select a canonical row from an empty group$",
    ):
        select_canonical_row([])


def test_promote_artifact_moves_staged_file_and_reuses_identical_target(tmp_path: Path) -> None:
    stage = tmp_path / "stage"
    root = tmp_path / "root"
    staged = stage / "data" / "a.parquet"
    target = root / "data" / "a.parquet"
    staged.parent.mkdir(parents=True)
    staged.write_bytes(b"payload")
    expected_sha = hashlib.sha256(b"payload").hexdigest()

    assert _promote_artifact(stage, root, "data/a.parquet", expected_sha) is True
    assert target.read_bytes() == b"payload"
    assert _promote_artifact(stage, root, "data/a.parquet", expected_sha) is False

    with pytest.raises(DeduplicationError, match="missing staged artifact"):
        _promote_artifact(stage, root, "data/missing.parquet", "0" * 64)


def test_promote_artifact_allows_an_existing_target_parent(tmp_path: Path) -> None:
    stage = tmp_path / "stage"
    root = tmp_path / "root"
    staged = stage / "nested" / "a.parquet"
    (root / "nested").mkdir(parents=True)
    staged.parent.mkdir(parents=True)
    staged.write_bytes(b"payload")

    assert (
        _promote_artifact(
            stage,
            root,
            "nested/a.parquet",
            hashlib.sha256(b"payload").hexdigest(),
        )
        is True
    )


def test_promote_entry_reuses_identical_targets_when_staged_files_are_absent(
    tmp_path: Path,
) -> None:
    stage = tmp_path / "stage"
    root = tmp_path / "root"
    parquet = root / "data" / "a.parquet"
    manifest = root / "manifests" / "a.manifest.json"
    parquet.parent.mkdir(parents=True)
    manifest.parent.mkdir(parents=True)
    parquet.write_bytes(b"parquet")
    manifest.write_bytes(b"manifest")
    entry = {
        "parquet": "data/a.parquet",
        "manifest": "manifests/a.manifest.json",
        "parquet_sha256": file_sha256(parquet),
        "manifest_sha256": file_sha256(manifest),
    }

    assert _promote_entry(stage, root, entry, promotion_hook=None, promoted=0) == 0


def test_promote_entry_moves_both_files_and_reports_each_promotion(tmp_path: Path) -> None:
    stage = tmp_path / "stage"
    root = tmp_path / "root"
    parquet = stage / "data" / "a.parquet"
    manifest = stage / "manifests" / "a.manifest.json"
    parquet.parent.mkdir(parents=True)
    manifest.parent.mkdir(parents=True)
    parquet.write_bytes(b"parquet")
    manifest.write_bytes(b"manifest")
    entry = {
        "parquet": "data/a.parquet",
        "manifest": "manifests/a.manifest.json",
        "parquet_sha256": file_sha256(parquet),
        "manifest_sha256": file_sha256(manifest),
    }
    promoted: list[int] = []

    count = _promote_entry(
        stage,
        root,
        entry,
        promotion_hook=promoted.append,
        promoted=0,
    )

    assert count == 2
    assert promoted == [1, 2]
    assert (root / "data" / "a.parquet").read_bytes() == b"parquet"
    assert (root / "manifests" / "a.manifest.json").read_bytes() == b"manifest"


def test_promote_staged_removes_stage_after_all_entries(tmp_path: Path) -> None:
    root = tmp_path / "root"
    stage = root / ".work" / "dedup" / "token"
    parquet = stage / "data" / "a.parquet"
    manifest = stage / "manifests" / "a.manifest.json"
    parquet.parent.mkdir(parents=True)
    manifest.parent.mkdir(parents=True)
    parquet.write_bytes(b"parquet")
    manifest.write_bytes(b"manifest")
    state = {
        "stage_dir": ".work/dedup/token",
        "files": [
            {
                "parquet": "data/a.parquet",
                "manifest": "manifests/a.manifest.json",
                "parquet_sha256": file_sha256(parquet),
                "manifest_sha256": file_sha256(manifest),
            }
        ],
    }
    progress: list[int] = []

    _promote_staged(root, state, promotion_hook=progress.append)

    assert progress == [1, 2]
    assert not stage.exists()


def test_promote_staged_rejects_a_missing_stage_directory(tmp_path: Path) -> None:
    with pytest.raises(
        DeduplicationError,
        match=r"^staged deduplication directory is missing: .+missing$",
    ):
        _promote_staged(
            tmp_path / "root",
            {"stage_dir": ".work/dedup/missing", "files": []},
        )


def test_state_payload_records_policy_inputs_and_duplicate_delta(tmp_path: Path) -> None:
    context = _DeduplicationContext(
        data_root=tmp_path,
        state_path=tmp_path / "state.json",
        state=None,
        parquets=(),
        manifests={},
        inputs={"z.parquet": "z", "a.parquet": "a"},
        input_rows=7,
    )
    changed = [{"parquet": "data/a.parquet"}]

    payload = _state_payload(context, changed, 5)

    assert payload == {
        "schema_version": 1,
        "policy_version": 1,
        "policy_sha256": DEDUPLICATION_POLICY_SHA256,
        "status": "staged",
        "inputs": {"a.parquet": "a", "z.parquet": "z"},
        "outputs": {},
        "input_rows": 7,
        "output_rows": 5,
        "duplicate_rows": 2,
        "files": changed,
    }


def test_state_payload_marks_an_unchanged_pass_complete_and_preserves_outputs(
    tmp_path: Path,
) -> None:
    context = _DeduplicationContext(
        data_root=tmp_path,
        state_path=tmp_path / "state.json",
        state=None,
        parquets=(),
        manifests={},
        inputs={"b.parquet": "b", "a.parquet": "a"},
        input_rows=4,
    )

    payload = _state_payload(context, [], 4)

    assert payload["status"] == "complete"
    assert payload["outputs"] == {"a.parquet": "a", "b.parquet": "b"}
    assert payload["duplicate_rows"] == 0


def test_complete_result_reuses_complete_state_and_current_output_count(tmp_path: Path) -> None:
    parquet = tmp_path / "a.parquet"
    write_geoparquet(_two_records(), parquet)
    inputs = {"a.parquet": file_sha256(parquet)}
    state = {
        "status": "complete",
        "policy_sha256": DEDUPLICATION_POLICY_SHA256,
        "outputs": inputs,
        "input_rows": 3,
        "duplicate_rows": 1,
    }

    result = _complete_result(state, inputs, (parquet,))

    assert result is not None
    assert result.status == "skipped"
    assert result.input_rows == 3
    assert result.output_rows == 2
    assert result.duplicate_rows == 1


def test_finish_deduplication_writes_complete_state_without_changes(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    context = _DeduplicationContext(
        data_root=tmp_path,
        state_path=tmp_path / "state.json",
        state=None,
        parquets=(),
        manifests={},
        inputs={"a.parquet": "sha"},
        input_rows=2,
    )
    writes: list[tuple[Path, dict[str, object]]] = []
    monkeypatch.setattr(
        dedup_module,
        "_write_state",
        lambda path, payload: writes.append((path, dict(payload))),
    )

    result = _finish_deduplication(context, tmp_path / "stage", {}, [], 2, None)

    assert result.status == "skipped"
    assert result.input_rows == 2
    assert result.output_rows == 2
    assert writes == [(context.state_path, {"outputs": {"a.parquet": "sha"}})]
