"""The published row now carries the splitting outcome as well as the detection.

A consumer reads one row per description, so the sentences and the reason there
are none have to travel in that row. A row that was refused for its language
must be distinguishable from one detection never settled, and from one that was
split into no sentences at all.
"""

from __future__ import annotations

from pathlib import Path

import pyarrow as pa
import pytest

from osm_polygon_description_tag.dataset.languages.annotations import (
    ANNOTATION_SCHEMA,
    ANNOTATION_SCHEMA_VERSION,
    AnnotationError,
    DescriptionAnnotation,
    annotation_table,
    read_annotation_part,
    write_annotation_part,
)
from osm_polygon_description_tag.dataset.languages.models import LanguageResult, LanguageStatus
from osm_polygon_description_tag.dataset.languages.records import DescriptionEntry
from osm_polygon_description_tag.dataset.sentences.models import (
    SentenceSplitStatus,
    not_detected_result,
    split_result,
    unsupported_language_result,
)

_SNAPSHOT = "a" * 64
_FINGERPRINT = "b" * 64


def _entry(osm_id: int = 7, text: str = "One. Two.") -> DescriptionEntry:
    return DescriptionEntry("region.osm.pbf", "way", osm_id, "description", text)


def _detected(code: str = "eng") -> LanguageResult:
    return LanguageResult(code, 0.9, 0.1, 0.8, LanguageStatus.DETECTED, "detected")


def _table(*annotations: DescriptionAnnotation) -> pa.Table:
    return annotation_table(
        annotations, snapshot_id=_SNAPSHOT, model_config_fingerprint=_FINGERPRINT
    )


def test_the_schema_version_moved_on_when_the_split_columns_arrived() -> None:
    """A reader must be able to tell a split-bearing part from a detection-only one."""
    assert ANNOTATION_SCHEMA_VERSION == 2
    names = ANNOTATION_SCHEMA.names
    assert names[-4:] == ["split_status", "split_reason", "sentence_count", "sentences"]
    assert ANNOTATION_SCHEMA.field("sentences").type == pa.list_(pa.string())
    for name in ("split_status", "split_reason", "sentence_count", "sentences"):
        assert not ANNOTATION_SCHEMA.field(name).nullable


def test_a_split_description_publishes_its_sentences_and_their_count() -> None:
    annotation = DescriptionAnnotation(_entry(), _detected(), split_result(("One. ", "Two.")))

    row = _table(annotation).to_pylist()[0]

    assert row["split_status"] == str(SentenceSplitStatus.SPLIT)
    assert row["split_reason"] == "split_sat_3l_sm"
    assert row["sentences"] == ["One. ", "Two."]
    assert row["sentence_count"] == 2


def test_an_unsupported_language_publishes_no_sentences_and_says_why() -> None:
    annotation = DescriptionAnnotation(
        _entry(text="Jedan. Dva."),
        _detected("hrv"),
        unsupported_language_result("hrv"),
    )

    row = _table(annotation).to_pylist()[0]

    assert row["language_code"] == "hrv"
    assert row["split_status"] == str(SentenceSplitStatus.UNSUPPORTED_LANGUAGE)
    assert row["split_reason"] == "unsupported_language_hrv"
    assert row["sentences"] == []
    assert row["sentence_count"] == 0


def test_an_undetected_description_is_distinguishable_from_an_unsupported_one() -> None:
    undetected = LanguageResult(None, None, None, None, LanguageStatus.UNCERTAIN, "tie")
    annotation = DescriptionAnnotation(
        _entry(), undetected, not_detected_result(str(LanguageStatus.UNCERTAIN))
    )

    row = _table(annotation).to_pylist()[0]

    assert row["split_status"] == str(SentenceSplitStatus.NOT_DETECTED)
    assert row["split_reason"] == "not_detected_uncertain"
    assert row["sentence_count"] == 0


def test_a_row_whose_sentence_count_disagrees_with_its_sentences_is_refused() -> None:
    """The count is redundant on purpose, so a corrupted part cannot pass silently."""
    table = _table(DescriptionAnnotation(_entry(), _detected(), split_result(("One.",))))
    tampered = table.set_column(
        table.schema.get_field_index("sentence_count"),
        ANNOTATION_SCHEMA.field("sentence_count"),
        pa.array([99], pa.int32()),
    )

    with pytest.raises(AnnotationError) as caught:
        _validate(tampered)

    assert str(caught.value) == "annotation row 0 has a wrong sentence_count"


def test_a_row_that_carries_sentences_without_the_split_status_is_refused() -> None:
    """Only a split row may carry sentences, in the file as well as in memory."""
    table = _table(
        DescriptionAnnotation(_entry(), _detected("hrv"), unsupported_language_result("hrv"))
    )
    smuggled = table.set_column(
        table.schema.get_field_index("sentences"),
        ANNOTATION_SCHEMA.field("sentences"),
        pa.array([["smuggled"]], pa.list_(pa.string())),
    ).set_column(
        table.schema.get_field_index("sentence_count"),
        ANNOTATION_SCHEMA.field("sentence_count"),
        pa.array([1], pa.int32()),
    )

    with pytest.raises(AnnotationError) as caught:
        _validate(smuggled)

    assert str(caught.value) == "annotation row 0 has sentences without a split status"


def test_a_written_part_round_trips_its_sentences(tmp_path: Path) -> None:
    """The sentences must survive Parquet exactly, including their whitespace."""
    table = _table(DescriptionAnnotation(_entry(), _detected(), split_result(("One. ", "Two."))))
    path = tmp_path / "part.parquet"

    write_annotation_part(path, table)

    assert read_annotation_part(path).to_pylist()[0]["sentences"] == ["One. ", "Two."]


def _validate(table: pa.Table) -> None:
    from osm_polygon_description_tag.dataset.languages.annotations import (
        validate_annotation_table,
    )

    validate_annotation_table(table, snapshot_id=_SNAPSHOT, model_config_fingerprint=_FINGERPRINT)


@pytest.mark.parametrize(
    ("column", "value", "message"),
    [
        ("sentences", pa.array([None], pa.list_(pa.string())), "has invalid sentences"),
        (
            "split_status",
            pa.array(["invented"], pa.string()),
            "has an unsupported split_status",
        ),
        ("split_reason", pa.array([""], pa.string()), "has an empty split_reason"),
    ],
)
def test_a_row_with_a_damaged_split_column_is_refused_by_name(
    column: str, value: pa.Array, message: str
) -> None:
    """Each split column has its own refusal so a damaged part names its own fault."""
    table = _table(DescriptionAnnotation(_entry(), _detected(), split_result(())))
    damaged = table.set_column(
        table.schema.get_field_index(column), ANNOTATION_SCHEMA.field(column), value
    )

    with pytest.raises(AnnotationError) as caught:
        _validate(damaged)

    assert str(caught.value) == f"annotation row 0 {message}"


def test_a_row_whose_sentences_are_not_text_is_refused() -> None:
    """Arrow can hold a list of anything; only a list of strings may be published."""
    table = _table(DescriptionAnnotation(_entry(), _detected(), split_result(("One.",))))
    damaged = table.set_column(
        table.schema.get_field_index("sentences"),
        ANNOTATION_SCHEMA.field("sentences"),
        pa.array([[None]], pa.list_(pa.string())),
    )

    with pytest.raises(AnnotationError) as caught:
        _validate(damaged)

    assert str(caught.value) == "annotation row 0 has invalid sentences"
