"""RED tests for deterministic cross-PBF OSM identity deduplication."""

import hashlib
import json
from datetime import UTC, datetime
from pathlib import Path

import pytest
from shapely.geometry import Polygon

import osm_polygon_description_tag.dataset.deduplication as dedup_module
from osm_polygon_description_tag.dataset.deduplication import (
    DEDUPLICATION_POLICY_SHA256,
    DUPLICATE_REJECTION_REASON,
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
from osm_polygon_description_tag.dataset.schema import SCHEMA
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
        {key: base.get(key) for key in SCHEMA.names if key != "source_pbf"},
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
        "timestamp": datetime(2026, 1, 1, tzinfo=UTC),
    }
    payload = json.dumps(
        {key: row.get(key) for key in SCHEMA.names if key != "source_pbf"},
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
        ("not-a-timestamp", 0.0),
        ("", 0.0),
        (None, 0.0),
        (42, 0.0),
    ],
)
def test_timestamp_rank_normalizes_supported_and_invalid_values(
    value: object, expected: float
) -> None:
    assert _timestamp_rank(value) == expected


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
