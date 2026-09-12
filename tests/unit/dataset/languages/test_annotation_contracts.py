"""Exact refusal contracts of the annotation part reader and writer.

Every message here names the file the operator has to look at. A substring
assertion still passes when that name is lost or corrupted, which is exactly
the information a Grid'5000 operator needs to find a bad part among the 386
shards, so these assert the whole message.
"""

from pathlib import Path

import pyarrow as pa
import pyarrow.parquet as pq
import pytest

from osm_polygon_description_tag.dataset.languages.annotations import (
    ANNOTATION_SCHEMA,
    AnnotationError,
    annotation_table,
    read_annotation_part,
    validate_annotation_table,
    validate_annotation_table_without_reserving,
    write_annotation_part,
)
from osm_polygon_description_tag.dataset.languages.models import LanguageResult, LanguageStatus
from osm_polygon_description_tag.dataset.languages.records import DescriptionEntry

_SNAPSHOT = "a" * 64
_FINGERPRINT = "b" * 64


def _entry(osm_id: int = 7, text: str = "A description") -> DescriptionEntry:
    return DescriptionEntry("region.osm.pbf", "way", osm_id, "description", text)


def _detected() -> LanguageResult:
    return LanguageResult("eng", 0.9, 0.1, 0.8, LanguageStatus.DETECTED, "detected")


def _table(count: int = 1) -> pa.Table:
    return annotation_table(
        [(_entry(osm_id=index + 1), _detected()) for index in range(count)],
        snapshot_id=_SNAPSHOT,
        model_config_fingerprint=_FINGERPRINT,
    )


def test_an_off_schema_write_is_refused_with_its_exact_reason() -> None:
    table = pa.table({"unexpected": pa.array([1])})

    with pytest.raises(AnnotationError) as caught:
        write_annotation_part(Path("unused.parquet"), table)

    assert str(caught.value) == "annotation table does not match the annotation schema"


def test_an_in_memory_table_is_reported_under_its_placeholder_name() -> None:
    """The placeholder stands in for a path, so it must survive verbatim."""
    off_schema = ANNOTATION_SCHEMA.remove(0).insert(0, pa.field("renamed", pa.string()))
    table = _table().rename_columns(off_schema.names)

    with pytest.raises(AnnotationError) as caught:
        validate_annotation_table(
            table, snapshot_id=_SNAPSHOT, model_config_fingerprint=_FINGERPRINT
        )

    assert (
        str(caught.value) == "annotation part has unexpected columns: <in-memory annotation table>"
    )


def test_an_off_schema_part_file_is_reported_under_its_own_path(tmp_path: Path) -> None:
    path = tmp_path / "part-00000000000000000000.parquet"
    pq.write_table(pa.table({"unexpected": pa.array(["x"])}), path)

    with pytest.raises(AnnotationError) as caught:
        read_annotation_part(path)

    assert str(caught.value) == f"annotation part has unexpected columns: {path}"


def test_a_written_part_is_bound_to_the_run_identity_of_its_first_row(tmp_path: Path) -> None:
    """A single-row part must still be validated against its own identity.

    Reading the binding from anything but row zero of a one-row table is out of
    bounds, and reading more rows than that to find it wastes the batch.
    """
    path = tmp_path / "part-00000000000000000000.parquet"

    digest = write_annotation_part(path, _table(count=1))

    table = read_annotation_part(path)
    assert table.num_rows == 1
    assert table.column("snapshot_id")[0].as_py() == _SNAPSHOT
    assert table.column("model_config_fingerprint")[0].as_py() == _FINGERPRINT
    assert len(digest) == 64


def test_a_part_whose_rows_disagree_on_the_run_identity_is_refused(
    tmp_path: Path,
) -> None:
    """Row zero supplies the binding, so a later divergent row must be caught."""
    other = annotation_table(
        [(_entry(osm_id=99), _detected())],
        snapshot_id="c" * 64,
        model_config_fingerprint=_FINGERPRINT,
    )
    mixed = pa.concat_tables([_table(count=1), other])

    with pytest.raises(AnnotationError) as caught:
        write_annotation_part(tmp_path / "part-00000000000000000000.parquet", mixed)

    assert str(caught.value) == "annotation row 1 has a different snapshot id"


def test_an_empty_table_is_written_without_consulting_a_row(tmp_path: Path) -> None:
    path = tmp_path / "part-00000000000000000000.parquet"

    digest = write_annotation_part(path, _table(count=0))

    assert read_annotation_part(path).num_rows == 0
    assert len(digest) == 64


def test_validating_without_reserving_leaves_the_callers_identity_set_untouched() -> None:
    """The caller reserves after its own commit, so nothing may be reserved here."""
    entry = _entry()
    seen: set[str] = set()

    identities = validate_annotation_table_without_reserving(
        annotation_table(
            [(entry, _detected())],
            snapshot_id=_SNAPSHOT,
            model_config_fingerprint=_FINGERPRINT,
        ),
        snapshot_id=_SNAPSHOT,
        model_config_fingerprint=_FINGERPRINT,
        seen_identities=seen,
    )

    assert identities == (entry.description_identity,)
    assert seen == set()


def test_validating_without_reserving_still_refuses_an_identity_already_seen() -> None:
    """Membership is still checked; only the reservation is deferred."""
    entry = _entry()
    seen = {entry.description_identity}

    with pytest.raises(AnnotationError) as caught:
        validate_annotation_table_without_reserving(
            annotation_table(
                [(entry, _detected())],
                snapshot_id=_SNAPSHOT,
                model_config_fingerprint=_FINGERPRINT,
            ),
            snapshot_id=_SNAPSHOT,
            model_config_fingerprint=_FINGERPRINT,
            seen_identities=seen,
        )

    assert "duplicate" in str(caught.value)


def test_an_explicit_no_merge_request_also_leaves_the_identity_set_untouched() -> None:
    """``merge_seen=False`` remains part of the public contract."""
    entry = _entry()
    seen: set[str] = set()

    validate_annotation_table(
        annotation_table(
            [(entry, _detected())],
            snapshot_id=_SNAPSHOT,
            model_config_fingerprint=_FINGERPRINT,
        ),
        snapshot_id=_SNAPSHOT,
        model_config_fingerprint=_FINGERPRINT,
        seen_identities=seen,
        merge_seen=False,
    )

    assert seen == set()
