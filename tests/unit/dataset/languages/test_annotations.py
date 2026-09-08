"""The frozen annotation schema and its part files."""

import hashlib
from pathlib import Path

import pyarrow as pa
import pyarrow.parquet as pq
import pytest

from osm_polygon_description_tag.dataset.languages.annotations import (
    ANNOTATION_SCHEMA,
    AnnotationError,
    annotation_identities,
    annotation_row,
    annotation_table,
    read_annotation_part,
    validate_annotation_schema,
    validate_annotation_table,
    write_annotation_part,
)
from osm_polygon_description_tag.dataset.languages.models import LanguageResult, LanguageStatus
from osm_polygon_description_tag.dataset.languages.records import DescriptionEntry

SNAPSHOT = "a" * 64
FINGERPRINT = "b" * 64


def _entry(tag_key: str = "description", text: str = "A description") -> DescriptionEntry:
    return DescriptionEntry("region.osm.pbf", "way", 7, tag_key, text)


def _detected() -> LanguageResult:
    return LanguageResult("eng", 0.9, 0.1, 0.8, LanguageStatus.DETECTED, "detected")


def _table(pairs: list[tuple[DescriptionEntry, LanguageResult]]) -> pa.Table:
    return annotation_table(pairs, snapshot_id=SNAPSHOT, model_config_fingerprint=FINGERPRINT)


def test_a_row_carries_identity_provenance_text_and_result() -> None:
    entry = _entry()

    row = annotation_row(
        entry, _detected(), snapshot_id=SNAPSHOT, model_config_fingerprint=FINGERPRINT
    )

    assert row["description_identity"] == entry.description_identity
    assert row["text_sha256"] == entry.text_sha256
    assert row["source_pbf"] == "region.osm.pbf"
    assert row["osm_id"] == 7
    assert row["original_text"] == "A description"
    assert row["status"] == "detected"
    assert row["language_code"] == "eng"
    assert row["snapshot_id"] == SNAPSHOT
    assert row["model_config_fingerprint"] == FINGERPRINT


def test_a_non_linguistic_result_leaves_every_score_null() -> None:
    result = LanguageResult(None, None, None, None, LanguageStatus.NON_LINGUISTIC, "no_letters")

    row = annotation_row(
        _entry(text="123"), result, snapshot_id=SNAPSHOT, model_config_fingerprint=FINGERPRINT
    )

    assert row["language_code"] is None
    assert row["top_score"] is None
    assert row["runner_up_score"] is None
    assert row["margin"] is None
    assert row["status"] == "non_linguistic"


def test_a_table_matches_the_frozen_schema_and_row_order() -> None:
    pairs = [(_entry(), _detected()), (_entry("description:fr", "Le mur"), _detected())]

    table = _table(pairs)

    assert table.schema == ANNOTATION_SCHEMA
    assert table.num_rows == 2
    assert table.column("tag_key").to_pylist() == ["description", "description:fr"]
    assert annotation_identities(table) == [entry.description_identity for entry, _ in pairs]


def test_an_empty_table_still_matches_the_schema() -> None:
    table = _table([])

    assert table.schema == ANNOTATION_SCHEMA
    assert table.num_rows == 0
    assert annotation_identities(table) == []


def test_a_part_round_trips_and_reports_its_checksum(tmp_path: Path) -> None:
    table = _table([(_entry(), _detected())])
    path = tmp_path / "part.parquet"

    digest = write_annotation_part(path, table)

    assert digest == hashlib.sha256(path.read_bytes()).hexdigest()
    assert read_annotation_part(path).equals(table)
    group = pq.ParquetFile(path).metadata.row_group(0)
    columns = [group.column(index) for index in range(group.num_columns)]
    assert all(column.compression == "ZSTD" for column in columns)
    assert [
        column.path_in_schema for column in columns if "RLE_DICTIONARY" in column.encodings
    ] == ["osm_type", "tag_key", "language_code", "status", "reason"]
    assert [item.name for item in tmp_path.glob("*.parquet")] == ["part.parquet"]
    assert list(tmp_path.glob(".*.tmp")) == []


def test_writing_a_foreign_table_is_refused(tmp_path: Path) -> None:
    foreign = pa.table({"description_identity": ["x"]})

    with pytest.raises(AnnotationError, match="does not match the annotation schema"):
        write_annotation_part(tmp_path / "part.parquet", foreign)
    assert not (tmp_path / "part.parquet").exists()


def test_an_unreadable_part_is_reported(tmp_path: Path) -> None:
    path = tmp_path / "part.parquet"
    path.write_bytes(b"not parquet at all")

    with pytest.raises(AnnotationError, match="cannot read annotation part"):
        read_annotation_part(path)

    with pytest.raises(AnnotationError, match="cannot read annotation part"):
        read_annotation_part(tmp_path / "absent.parquet")


def test_a_part_with_unexpected_columns_is_rejected(tmp_path: Path) -> None:
    path = tmp_path / "part.parquet"
    pq.write_table(pa.table({"description_identity": ["x"]}), path)

    with pytest.raises(AnnotationError, match="unexpected columns"):
        read_annotation_part(path)


def test_a_part_with_a_changed_field_type_is_rejected(tmp_path: Path) -> None:
    table = _table([(_entry(), _detected())])
    index = table.schema.get_field_index("osm_id")
    altered = table.set_column(index, "osm_id", table.column("osm_id").cast(pa.int32()))
    path = tmp_path / "part.parquet"
    pq.write_table(altered, path)

    with pytest.raises(AnnotationError, match="field mismatch for osm_id"):
        read_annotation_part(path)


def test_a_nullability_change_is_rejected(tmp_path: Path) -> None:
    relaxed = pa.schema(
        [
            field.with_nullable(True) if field.name == "status" else field
            for field in ANNOTATION_SCHEMA
        ]
    )

    with pytest.raises(AnnotationError, match="field mismatch for status"):
        validate_annotation_schema(relaxed, tmp_path / "part.parquet")


def test_a_row_with_a_forged_identity_is_rejected(tmp_path: Path) -> None:
    table = _table([(_entry(), _detected())])
    altered = table.set_column(
        table.schema.get_field_index("description_identity"),
        table.schema.field("description_identity"),
        pa.array(["c" * 64], type=pa.string()),
    )

    with pytest.raises(AnnotationError, match="description_identity"):
        write_annotation_part(tmp_path / "part.parquet", altered)


def test_duplicate_description_identities_are_rejected() -> None:
    with pytest.raises(AnnotationError, match="duplicate description identity"):
        _table([(_entry(), _detected()), (_entry(), _detected())])


@pytest.mark.parametrize(
    ("field", "value", "message"),
    [
        ("snapshot_id", "c" * 64, "a different snapshot id"),
        ("model_config_fingerprint", "c" * 64, "a different model fingerprint"),
        ("text_sha256", "c" * 64, "an invalid text_sha256"),
        ("osm_type", "", "invalid entry fields"),
        ("original_text", None, "invalid entry fields"),
        ("status", "unknown", "invalid detection result"),
        ("top_score", 2.0, "invalid detection result"),
        ("language_code", None, "invalid detection result"),
    ],
)
def test_corrupt_cells_are_rejected_without_merging_identities(
    field: str, value: object, message: str
) -> None:
    rows = _table([(_entry(), _detected())]).to_pylist()
    rows[0][field] = value
    corrupt = pa.Table.from_pylist(rows, schema=ANNOTATION_SCHEMA)
    seen = {"previous-identity"}

    with pytest.raises(AnnotationError, match=f"annotation row 0 has {message}"):
        validate_annotation_table(
            corrupt,
            snapshot_id=SNAPSHOT,
            model_config_fingerprint=FINGERPRINT,
            seen_identities=seen,
        )
    assert seen == {"previous-identity"}


def test_inconsistent_margin_rejects_the_batch_without_merging_valid_prefix() -> None:
    rows = _table(
        [(_entry(), _detected()), (_entry("description:fr", "Le mur"), _detected())]
    ).to_pylist()
    rows[1]["margin"] = 0.1
    corrupt = pa.Table.from_pylist(rows, schema=ANNOTATION_SCHEMA)
    seen = {"previous-identity"}

    with pytest.raises(AnnotationError) as caught:
        validate_annotation_table(
            corrupt,
            snapshot_id=SNAPSHOT,
            model_config_fingerprint=FINGERPRINT,
            seen_identities=seen,
        )
    assert str(caught.value) == (
        "annotation row 1 has invalid detection result: "
        "margin must equal top score minus runner-up score"
    )
    assert seen == {"previous-identity"}


def test_external_part_with_repeated_rows_is_rejected_without_merging_history() -> None:
    valid = _table([(_entry(), _detected())])
    corrupt = pa.concat_tables([valid, valid])
    seen = {"previous-identity"}

    with pytest.raises(AnnotationError) as caught:
        validate_annotation_table(
            corrupt,
            snapshot_id=SNAPSHOT,
            model_config_fingerprint=FINGERPRINT,
            seen_identities=seen,
        )

    identity = valid["description_identity"][0].as_py()
    assert str(caught.value) == f"duplicate description identity at row 1: {identity}"
    assert seen == {"previous-identity"}


def test_validation_checks_prior_identities_without_copying_the_history() -> None:
    class _NonIterableSet:
        def __init__(self, identities: set[str]) -> None:
            self._identities = identities

        def __contains__(self, value: object) -> bool:
            return value in self._identities

        def __iter__(self):
            raise AssertionError("prior identity history must not be copied")

        def update(self, values: object) -> None:
            raise AssertionError("duplicate validation must not merge a failed part")

    entry = _entry()
    table = _table([(entry, _detected())])
    seen = _NonIterableSet({entry.description_identity})

    with pytest.raises(AnnotationError, match="duplicate description identity"):
        validate_annotation_table(
            table,
            snapshot_id=SNAPSHOT,
            model_config_fingerprint=FINGERPRINT,
            seen_identities=seen,  # type: ignore[arg-type]
        )


def test_validation_merges_new_identities_after_a_valid_batch() -> None:
    entry = _entry()
    seen: set[str] = set()

    identities = validate_annotation_table(
        _table([(entry, _detected())]),
        snapshot_id=SNAPSHOT,
        model_config_fingerprint=FINGERPRINT,
        seen_identities=seen,
    )

    assert identities == (entry.description_identity,)
    assert seen == {entry.description_identity}
