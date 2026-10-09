"""RED tests for deterministic cross-PBF OSM identity deduplication."""

import hashlib
import json
import shutil
from collections.abc import Mapping
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from unittest.mock import Mock, patch

import pytest
from shapely.geometry import Polygon

import osm_polygon_description_tag.dataset.deduplication as dedup_module
from osm_polygon_description_tag.dataset.canonical_rows import (
    CANONICAL_FINGERPRINT_COLUMNS,
    canonical_rows_sql,
)
from osm_polygon_description_tag.dataset.deduplication import (
    DEDUPLICATION_POLICY_SHA256,
    DUPLICATE_REJECTION_REASON,
    DeduplicationError,
    _complete_state_matches,
    _parse_timestamp,
    _row_fingerprint,
    _timestamp_rank,
    _version,
    deduplicate_dataset,
    select_canonical_row,
)
from osm_polygon_description_tag.dataset.manifest import (
    Manifest,
    RunCounts,
    output_identity_for,
    source_identity_for,
    write_manifest,
)
from osm_polygon_description_tag.dataset.storage import write_geoparquet
from tests.conftest import make_record_dict


def _write_source(
    data_root: Path,
    source_root: Path,
    name: str,
    records: list[dict[str, object]],
) -> None:
    source = source_root / f"{name}.osm.pbf"
    source.write_bytes(name.encode())
    output = data_root / "data" / f"{name}.parquet"
    manifest_path = data_root / "manifests" / f"{name}.manifest.json"
    rows = write_geoparquet(records, output)
    write_manifest(
        Manifest(
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
            counts=RunCounts(emitted_features=rows, included_rows=rows, rejections={}),
        ),
        manifest_path,
    )


def test_select_canonical_row_prefers_latest_osm_version_then_filename() -> None:
    rows = [
        {
            "osm_type": "way",
            "osm_id": 1,
            "version": 4,
            "timestamp": None,
            "source_pbf": "z.osm.pbf",
        },
        {
            "osm_type": "way",
            "osm_id": 1,
            "version": 5,
            "timestamp": None,
            "source_pbf": "z.osm.pbf",
        },
        {
            "osm_type": "way",
            "osm_id": 1,
            "version": 5,
            "timestamp": None,
            "source_pbf": "a.osm.pbf",
        },
    ]

    assert select_canonical_row(rows)["source_pbf"] == "a.osm.pbf"
    assert select_canonical_row(rows)["version"] == 5


def test_select_canonical_row_uses_version_before_timestamp_and_source() -> None:
    rows = [
        {
            "version": 1,
            "timestamp": "2026-01-01T00:00:00Z",
            "source_pbf": "a.osm.pbf",
        },
        {
            "version": 2,
            "timestamp": "2025-01-01T00:00:00Z",
            "source_pbf": "z.osm.pbf",
        },
    ]

    assert select_canonical_row(rows) is rows[1]


def test_select_canonical_row_uses_timestamp_before_source() -> None:
    rows = [
        {
            "version": 1,
            "timestamp": "2025-01-01T00:00:00Z",
            "source_pbf": "a.osm.pbf",
        },
        {
            "version": 1,
            "timestamp": "2026-01-01T00:00:00Z",
            "source_pbf": "z.osm.pbf",
        },
    ]

    assert select_canonical_row(rows) is rows[1]


def test_select_canonical_row_uses_empty_source_name_for_missing_source() -> None:
    missing_source = {"version": 1, "timestamp": None}
    explicit_source = {"version": 1, "timestamp": None, "source_pbf": "A.osm.pbf"}

    assert select_canonical_row([explicit_source, missing_source]) is missing_source


def test_text_aware_canonical_selection_filters_invalid_higher_ranked_rows() -> None:
    valid = {
        "osm_type": "way",
        "osm_id": 1,
        "version": 1,
        "timestamp": None,
        "source_pbf": "region.osm.pbf",
        "description": "valid",
        "localized_descriptions": [],
    }
    invalid = dict(valid, version=2, description=" ")

    assert select_canonical_row([invalid, valid], require_successful_text=True) is valid


def test_canonical_rows_sql_filters_before_ranking_and_keeps_default_unfiltered() -> None:
    columns = ("osm_type", "osm_id", "description", "localized_descriptions")
    unfiltered = canonical_rows_sql("all_features", columns, key_value_columns_are_maps=True)
    filtered = canonical_rows_sql(
        "all_features",
        columns,
        key_value_columns_are_maps=True,
        require_successful_text=True,
    )

    assert "WHERE (description IS NOT NULL" not in unfiltered
    assert "\n            \n        ) ranked" in unfiltered
    assert "WHERE (description IS NOT NULL" in filtered
    assert "FROM all_features\n            WHERE (description IS NOT NULL" in filtered


def test_deduplication_relation_requires_text_aware_canonical_selection() -> None:
    connection = Mock()
    with patch.object(dedup_module, "canonical_rows_sql", return_value="SELECT 1") as builder:
        dedup_module._canonical_relation(connection, [Path("a.parquet")])

    builder.assert_called_once()
    assert builder.call_args.kwargs["require_successful_text"] is True
    connection.execute.assert_called_once_with("CREATE TEMP TABLE deduplicated AS SELECT 1")


def test_canonical_tie_break_and_fingerprint_ignore_source_filename() -> None:
    base = {
        "osm_type": "way",
        "osm_id": 7,
        "version": 1,
        "timestamp": "2026-01-01T00:00:00Z",
        "source_pbf": "a.osm.pbf",
        "description": "same",
    }
    from_other_source = dict(base, source_pbf="z.osm.pbf")
    changed = dict(base, description="different")

    expected_payload = json.dumps(
        {key: base.get(key) for key in CANONICAL_FINGERPRINT_COLUMNS},
        ensure_ascii=False,
        sort_keys=True,
        default=str,
        separators=(",", ":"),
    )
    expected_fingerprint = hashlib.sha256(expected_payload.encode("utf-8")).hexdigest()
    assert _row_fingerprint(base) == expected_fingerprint
    assert _row_fingerprint(base) == _row_fingerprint(from_other_source)
    assert _row_fingerprint(base) != _row_fingerprint(changed)
    assert select_canonical_row([from_other_source, base]) == base
    assert select_canonical_row([changed, base]) in (changed, base)


def test_row_fingerprint_is_utf8_and_stringifies_non_json_values() -> None:
    row = {
        "description": "café",
        "name": datetime(2026, 1, 1, tzinfo=UTC),
    }
    payload = json.dumps(
        {key: row.get(key) for key in CANONICAL_FINGERPRINT_COLUMNS},
        ensure_ascii=False,
        sort_keys=True,
        default=str,
        separators=(",", ":"),
    )

    assert _row_fingerprint(row) == hashlib.sha256(payload.encode("utf-8")).hexdigest()


def test_row_fingerprint_requests_ascii_false(monkeypatch: pytest.MonkeyPatch) -> None:
    options: dict[str, object] = {}
    original_dumps = dedup_module.json.dumps

    def dumps(value: object, *args: object, **kwargs: object) -> str:
        options.update(kwargs)
        return original_dumps(value, *args, **kwargs)  # type: ignore[arg-type]

    monkeypatch.setattr(dedup_module.json, "dumps", dumps)

    _row_fingerprint({"description": "café"})

    assert options["ensure_ascii"] is False


@pytest.mark.parametrize(
    ("value", "expected"),
    [(None, -1), ("not-a-version", -1), (object(), -1), (0, 0), (2.5, 2)],
)
def test_version_uses_negative_one_for_non_numeric_values(value: object, expected: int) -> None:
    assert _version(value) == expected


@pytest.mark.parametrize(
    ("state", "current", "expected"),
    [
        (
            {
                "status": "complete",
                "policy_sha256": DEDUPLICATION_POLICY_SHA256,
                "outputs": {"a": "1"},
            },
            {"a": "1"},
            True,
        ),
        (
            {
                "status": "pending",
                "policy_sha256": DEDUPLICATION_POLICY_SHA256,
                "outputs": {"a": "1"},
            },
            {"a": "1"},
            False,
        ),
        (
            {"status": "complete", "policy_sha256": "wrong", "outputs": {"a": "1"}},
            {"a": "1"},
            False,
        ),
        (
            {
                "status": "complete",
                "policy_sha256": DEDUPLICATION_POLICY_SHA256,
                "outputs": {"a": "2"},
            },
            {"a": "1"},
            False,
        ),
    ],
)
def test_complete_state_requires_status_policy_and_exact_outputs(
    state: dict[str, object], current: dict[str, str], expected: bool
) -> None:
    assert _complete_state_matches(state, current) is expected


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        (datetime(2026, 1, 1, tzinfo=UTC), 1767225600.0),
        ("2026-01-01T01:00:00+01:00", 1767225600.0),
        ("2026-01-01T00:00:00", 1767225600.0),
        ("", float("-inf")),
        (None, float("-inf")),
    ],
)
def test_timestamp_rank_normalizes_supported_values_and_nulls(
    value: object, expected: float
) -> None:
    assert _timestamp_rank(value) == expected


@pytest.mark.parametrize("value", ["not-a-timestamp", 42])
def test_timestamp_rank_rejects_non_null_invalid_values(value: object) -> None:
    with pytest.raises(ValueError) as error:
        _timestamp_rank(value)
    assert str(error.value) == "timestamp must be null or a valid ISO-8601 value"


def test_parse_timestamp_returns_utc_aware_values_or_none() -> None:
    assert _parse_timestamp("2026-01-01T01:00:00+01:00") == datetime(2026, 1, 1, tzinfo=UTC)
    assert _parse_timestamp("2026-01-01T00:00:00Z") == datetime(2026, 1, 1, tzinfo=UTC)
    assert _parse_timestamp("2026-01-01T00:00:00z") is None
    assert _parse_timestamp("not-a-timestamp") is None


def test_deduplicate_dataset_rewrites_overlapping_rows_and_is_idempotent(tmp_path: Path) -> None:
    data_root = tmp_path / "generated"
    source_root = tmp_path / "raw"
    (data_root / "data").mkdir(parents=True)
    (data_root / "manifests").mkdir()
    source_root.mkdir()
    duplicate_a = make_record_dict(
        Polygon([(0, 0), (0, 1), (1, 1), (1, 0)]),
        {"description": "old"},
        osm_id=1,
        source_pbf="a.osm.pbf",
    )
    duplicate_b = dict(duplicate_a, source_pbf="b.osm.pbf", version=2, description="new")
    unique = make_record_dict(
        Polygon([(2, 2), (2, 3), (3, 3), (3, 2)]),
        {"description": "unique"},
        osm_id=2,
        source_pbf="b.osm.pbf",
    )
    _write_source(data_root, source_root, "a", [duplicate_a])
    _write_source(data_root, source_root, "b", [duplicate_b, unique])

    result = deduplicate_dataset(data_root)
    assert result.input_rows == 3
    assert result.output_rows == 2
    assert result.duplicate_rows == 1
    assert result.files_changed == 1

    import pyarrow.parquet as pq

    assert pq.read_table(data_root / "data" / "a.parquet").num_rows == 0
    table_b = pq.read_table(data_root / "data" / "b.parquet")
    assert table_b.num_rows == 2
    manifest_a = Manifest.from_payload(
        __import__("json").loads((data_root / "manifests" / "a.manifest.json").read_text())
    )
    assert manifest_a.counts.rejections == {DUPLICATE_REJECTION_REASON: 1}

    second = deduplicate_dataset(data_root)
    assert second.status == "skipped"
    assert second.output_rows == 2


def test_deduplicate_dataset_spills_under_data_root_work_directory(tmp_path: Path) -> None:
    data_root = tmp_path / "generated"
    source_root = tmp_path / "raw"
    (data_root / "data").mkdir(parents=True)
    (data_root / "manifests").mkdir()
    source_root.mkdir()
    record = make_record_dict(
        Polygon([(0, 0), (0, 1), (1, 1), (1, 0)]),
        {"description": "only"},
        osm_id=1,
        source_pbf="a.osm.pbf",
    )
    _write_source(data_root, source_root, "a", [record])

    deduplicate_dataset(data_root)

    assert (data_root / ".work" / "duckdb").is_dir()


def test_deduplicate_dataset_resumes_after_promotion_interrupt(tmp_path: Path) -> None:
    data_root = tmp_path / "generated"
    source_root = tmp_path / "raw"
    (data_root / "data").mkdir(parents=True)
    (data_root / "manifests").mkdir()
    source_root.mkdir()
    first = make_record_dict(
        Polygon([(0, 0), (0, 1), (1, 1), (1, 0)]),
        {"description": "one"},
        osm_id=1,
        source_pbf="a.osm.pbf",
    )
    duplicate = dict(first, source_pbf="b.osm.pbf", version=2)
    _write_source(data_root, source_root, "a", [first])
    _write_source(data_root, source_root, "b", [duplicate])

    def interrupt(count: int) -> None:
        if count == 1:
            raise KeyboardInterrupt

    with pytest.raises(KeyboardInterrupt):
        deduplicate_dataset(data_root, promotion_hook=interrupt)

    resumed = deduplicate_dataset(data_root)
    assert resumed.status == "deduplicated"
    assert resumed.output_rows == 1


@pytest.mark.parametrize("error_type", [RuntimeError, KeyboardInterrupt])
@pytest.mark.parametrize("creates_stage_dir", [True, False])
def test_deduplicate_dataset_removes_stage_dir_when_staging_fails(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    error_type: type[BaseException],
    creates_stage_dir: bool,
) -> None:
    data_root = tmp_path / "generated"
    source_root = tmp_path / "raw"
    (data_root / "data").mkdir(parents=True)
    (data_root / "manifests").mkdir()
    source_root.mkdir()
    first = make_record_dict(
        Polygon([(0, 0), (0, 1), (1, 1), (1, 0)]),
        {"description": "one"},
        osm_id=1,
        source_pbf="a.osm.pbf",
    )
    duplicate = dict(first, source_pbf="b.osm.pbf", version=2)
    _write_source(data_root, source_root, "a", [first])
    _write_source(data_root, source_root, "b", [duplicate])

    real_stage_changes = dedup_module._stage_changes

    def failing_stage_changes(context: object, stage_root: Path) -> object:
        if creates_stage_dir:
            (stage_root / "data").mkdir(parents=True)
            (stage_root / "data" / "partial.parquet").write_bytes(b"partial")
        raise error_type("staging failed")

    monkeypatch.setattr(dedup_module, "_stage_changes", failing_stage_changes)
    with pytest.raises(error_type, match="staging failed"):
        deduplicate_dataset(data_root)
    monkeypatch.setattr(dedup_module, "_stage_changes", real_stage_changes)

    dedup_root = data_root / ".work" / "dedup"
    leftovers = list(dedup_root.iterdir()) if dedup_root.exists() else []
    assert leftovers == []
    assert not (data_root / dedup_module._STATE_RELATIVE_PATH).exists()

    result = deduplicate_dataset(data_root)
    assert result.status == "deduplicated"
    assert result.output_rows == 1


def _overlapping_dataset(tmp_path: Path) -> Path:
    data_root = tmp_path / "generated"
    source_root = tmp_path / "raw"
    (data_root / "data").mkdir(parents=True)
    (data_root / "manifests").mkdir()
    source_root.mkdir()
    first = make_record_dict(
        Polygon([(0, 0), (0, 1), (1, 1), (1, 0)]),
        {"description": "one"},
        osm_id=1,
        source_pbf="a.osm.pbf",
    )
    duplicate = dict(first, source_pbf="b.osm.pbf", version=2)
    _write_source(data_root, source_root, "a", [first])
    _write_source(data_root, source_root, "b", [duplicate])
    return data_root


def _fail_state_write(
    monkeypatch: pytest.MonkeyPatch,
    status: str,
    *,
    persist_first: bool,
    error: BaseException,
) -> None:
    """Raise ``error`` when a state with ``status`` is written, optionally after persisting it."""
    real_write_state = dedup_module._write_state

    def failing_write_state(path: Path, payload: Mapping[str, object]) -> None:
        if payload.get("status") != status:
            real_write_state(path, payload)
            return
        if persist_first:
            real_write_state(path, payload)
        raise error

    monkeypatch.setattr(dedup_module, "_write_state", failing_write_state)


def _interrupt_first_promotion(count: int) -> None:
    if count == 1:
        raise KeyboardInterrupt


@pytest.mark.parametrize("persist_first", [False, True])
def test_deduplicate_dataset_removes_stage_dir_when_staged_state_is_not_recorded(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    persist_first: bool,
) -> None:
    data_root = _overlapping_dataset(tmp_path)
    _fail_state_write(
        monkeypatch,
        "staged",
        persist_first=persist_first,
        error=OSError("no space left on device"),
    )

    with pytest.raises(OSError, match="no space left on device"):
        deduplicate_dataset(data_root)
    monkeypatch.undo()

    stage_root = data_root / ".work" / "dedup"
    leftovers = list(stage_root.iterdir()) if stage_root.exists() else []
    # A recorded state names its directory, so the next run must resume it.
    assert (data_root / dedup_module._STATE_RELATIVE_PATH).exists() is persist_first
    assert len(leftovers) == (1 if persist_first else 0)
    result = deduplicate_dataset(data_root)
    assert result.status == "deduplicated"
    assert result.output_rows == 1


def test_deduplicate_dataset_resumes_when_crash_precedes_completion_record(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    data_root = _overlapping_dataset(tmp_path)
    _fail_state_write(monkeypatch, "complete", persist_first=False, error=KeyboardInterrupt())

    with pytest.raises(KeyboardInterrupt):
        deduplicate_dataset(data_root)
    monkeypatch.undo()

    state_path = data_root / dedup_module._STATE_RELATIVE_PATH
    assert json.loads(state_path.read_text(encoding="utf-8"))["status"] == "staged"
    result = deduplicate_dataset(data_root)
    assert result.status == "deduplicated"
    assert result.output_rows == 1


def test_deduplicate_dataset_completes_when_crash_follows_completion_record(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    data_root = _overlapping_dataset(tmp_path)
    _fail_state_write(monkeypatch, "complete", persist_first=True, error=KeyboardInterrupt())

    with pytest.raises(KeyboardInterrupt):
        deduplicate_dataset(data_root)
    monkeypatch.undo()

    result = deduplicate_dataset(data_root)
    assert result.status == "skipped"
    assert result.output_rows == 1
    assert list((data_root / ".work" / "dedup").iterdir()) == []


def test_deduplicate_dataset_removes_orphan_stage_entries_from_earlier_runs(
    tmp_path: Path,
) -> None:
    data_root = _overlapping_dataset(tmp_path)
    assert deduplicate_dataset(data_root).status == "deduplicated"
    stage_root = data_root / ".work" / "dedup"
    orphan_dir = stage_root / "0123abcd"
    (orphan_dir / "data").mkdir(parents=True)
    (orphan_dir / "data" / "a.parquet").write_bytes(b"orphan")
    (stage_root / "stray.tmp").write_bytes(b"stray")

    result = deduplicate_dataset(data_root)

    assert result.status == "skipped"
    assert list(stage_root.iterdir()) == []


def test_deduplicate_dataset_skips_orphan_stage_entries_it_cannot_remove(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    data_root = _overlapping_dataset(tmp_path)
    assert deduplicate_dataset(data_root).status == "deduplicated"
    stage_root = data_root / ".work" / "dedup"
    orphan_dir = stage_root / "0123abcd"
    (orphan_dir / "data").mkdir(parents=True)
    (orphan_dir / "data" / "a.parquet").write_bytes(b"orphan owned by another uid")
    (stage_root / "stray.tmp").write_bytes(b"stray")
    real_rmtree = dedup_module.shutil.rmtree

    def rmtree_refusing_orphan(path: Path, **kwargs: Any) -> None:
        if path == orphan_dir:
            raise PermissionError(13, "Permission denied", str(path))
        real_rmtree(path, **kwargs)

    monkeypatch.setattr(dedup_module.shutil, "rmtree", rmtree_refusing_orphan)

    result = deduplicate_dataset(data_root)

    assert result.status == "skipped"
    assert result.output_rows == 1
    assert orphan_dir.is_dir()
    assert not (stage_root / "stray.tmp").exists()


def test_deduplicate_dataset_keeps_the_staged_dir_named_by_the_state(tmp_path: Path) -> None:
    data_root = _overlapping_dataset(tmp_path)
    with pytest.raises(KeyboardInterrupt):
        deduplicate_dataset(data_root, promotion_hook=_interrupt_first_promotion)
    state_path = data_root / dedup_module._STATE_RELATIVE_PATH
    staged_dir = data_root / json.loads(state_path.read_text(encoding="utf-8"))["stage_dir"]
    orphan_dir = data_root / ".work" / "dedup" / "0123abcd"
    orphan_dir.mkdir()

    with pytest.raises(KeyboardInterrupt):
        deduplicate_dataset(data_root, promotion_hook=_interrupt_first_promotion)

    assert staged_dir.is_dir()
    assert not orphan_dir.exists()
    result = deduplicate_dataset(data_root)
    assert result.status == "deduplicated"
    assert result.output_rows == 1


def test_failed_staged_state_write_reports_its_error_when_stage_dir_is_already_gone(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    data_root = _overlapping_dataset(tmp_path)
    real_write_state = dedup_module._write_state

    def write_after_stage_dir_vanishes(path: Path, payload: Mapping[str, object]) -> None:
        if payload.get("status") != "staged":
            real_write_state(path, payload)
            return
        shutil.rmtree(data_root / str(payload["stage_dir"]))
        raise OSError("no space left on device")

    monkeypatch.setattr(dedup_module, "_write_state", write_after_stage_dir_vanishes)

    # Cleaning up a directory that is already missing must not replace the write error.
    with pytest.raises(OSError, match="no space left on device"):
        deduplicate_dataset(data_root)
    monkeypatch.undo()

    assert not (data_root / dedup_module._STATE_RELATIVE_PATH).exists()
    result = deduplicate_dataset(data_root)
    assert result.status == "deduplicated"
    assert result.output_rows == 1


def test_unreadable_state_after_failed_write_keeps_the_staged_dir(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    data_root = _overlapping_dataset(tmp_path)
    real_write_state = dedup_module._write_state
    named_dirs: list[Path] = []

    def write_torn_state(path: Path, payload: Mapping[str, object]) -> None:
        if payload.get("status") != "staged":
            real_write_state(path, payload)
            return
        named_dirs.append(data_root / str(payload["stage_dir"]))
        path.write_text('{"status": "st', encoding="utf-8")
        raise OSError("no space left on device")

    monkeypatch.setattr(dedup_module, "_write_state", write_torn_state)
    with pytest.raises(OSError, match="no space left on device"):
        deduplicate_dataset(data_root)
    monkeypatch.undo()

    # The unreadable state may still name the directory, so the next run must be able to resume it.
    assert len(named_dirs) == 1
    assert named_dirs[0].is_dir()


def test_deduplicate_dataset_refuses_staged_resume_after_input_drift(tmp_path: Path) -> None:
    data_root = tmp_path / "generated"
    source_root = tmp_path / "raw"
    (data_root / "data").mkdir(parents=True)
    (data_root / "manifests").mkdir()
    source_root.mkdir()
    first = make_record_dict(
        Polygon([(0, 0), (0, 1), (1, 1), (1, 0)]),
        {"description": "one"},
        osm_id=1,
        source_pbf="a.osm.pbf",
    )
    duplicate = dict(first, source_pbf="b.osm.pbf", version=2)
    _write_source(data_root, source_root, "a", [first])
    _write_source(data_root, source_root, "b", [duplicate])

    def interrupt(count: int) -> None:
        if count == 1:
            raise KeyboardInterrupt

    with pytest.raises(KeyboardInterrupt):
        deduplicate_dataset(data_root, promotion_hook=interrupt)

    untouched = data_root / "data" / "b.parquet"
    untouched.write_bytes(untouched.read_bytes() + b"drift")

    with pytest.raises(DeduplicationError) as error:
        deduplicate_dataset(data_root)

    assert str(error.value) == (
        "staged deduplication inputs changed; refusing to resume: b.parquet"
    )
