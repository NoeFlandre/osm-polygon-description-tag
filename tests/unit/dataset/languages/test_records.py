import hashlib
from copy import deepcopy

import pyarrow as pa
import pytest

from osm_polygon_description_tag.dataset.languages import (
    DescriptionEntry,
    DescriptionRecordError,
    extract_description_entries,
)


def _row(
    tags: object,
    *,
    source_pbf: str = "europe.osm.pbf",
    osm_id: int = 17,
) -> dict[str, object]:
    return {
        "source_pbf": source_pbf,
        "osm_type": "way",
        "osm_id": osm_id,
        "tags": tags,
    }


def test_extracts_exact_base_and_all_localized_tags_in_stable_order() -> None:
    text = "  Café e\u0301\nprès de l'eau — 東京  "
    row = _row(
        [
            {"key": "description:source", "value": "source text"},
            {"key": "name", "value": "ignored"},
            {"key": "description:en", "value": "English text"},
            {"key": "description", "value": text},
            {"key": "description_extra", "value": "ignored"},
            {"key": "description:", "value": "empty suffix"},
        ]
    )

    entries = extract_description_entries(row)

    assert [(entry.tag_key, entry.original_text) for entry in entries] == [
        ("description", text),
        ("description:en", "English text"),
        ("description:source", "source text"),
    ]
    assert entries[0].source_pbf == "europe.osm.pbf"
    assert entries[0].osm_type == "way"
    assert entries[0].osm_id == 17
    assert entries[0].text_sha256 == hashlib.sha256(text.encode("utf-8")).hexdigest()


def test_null_base_is_omitted_but_localized_only_rows_are_supported() -> None:
    row = _row(
        [
            {"key": "description", "value": None},
            {"key": "description:en", "value": "Localized only"},
        ]
    )

    entries = extract_description_entries(row)

    assert len(entries) == 1
    assert entries[0].tag_key == "description:en"
    assert entries[0].original_text == "Localized only"


def test_empty_and_whitespace_description_values_remain_actual_entries() -> None:
    row = _row(
        [
            {"key": "description", "value": ""},
            {"key": "description:en", "value": " \n\t"},
        ]
    )

    entries = extract_description_entries(row)

    assert [entry.original_text for entry in entries] == ["", " \n\t"]


def test_null_tags_and_rows_without_description_produce_no_entries() -> None:
    assert extract_description_entries(_row(None)) == ()
    assert extract_description_entries(_row([{"key": "name", "value": "Only a name"}])) == ()


@pytest.mark.parametrize(
    "tags",
    [
        [("description", "Café"), ("description:fr", "Le mur")],
        [["description", "Café"], ["description:fr", "Le mur"]],
        pa.scalar(
            [("description", "Café"), ("description:fr", "Le mur")],
            type=pa.map_(pa.string(), pa.string()),
        ),
    ],
)
def test_key_value_sequences_preserve_keys_and_unicode_values(tags: object) -> None:
    entries = extract_description_entries(_row(tags))

    assert [(entry.tag_key, entry.original_text) for entry in entries] == [
        ("description", "Café"),
        ("description:fr", "Le mur"),
    ]


@pytest.mark.parametrize("pair", [("description",), ("description", "text", "extra"), ("", "text")])
def test_invalid_key_value_sequences_are_rejected(pair: tuple[str, ...]) -> None:
    with pytest.raises(DescriptionRecordError):
        extract_description_entries(_row([pair]))


def test_localized_tags_sort_by_key_even_when_values_have_reverse_order() -> None:
    tags = [
        ("description:zz", "A first value"),
        ("description:aa", "Z last value"),
        ("description", "description"),
    ]

    assert [entry.tag_key for entry in extract_description_entries(_row(tags))] == [
        "description",
        "description:aa",
        "description:zz",
    ]


def test_arrow_list_of_structs_is_read_without_mutating_the_row() -> None:
    tags = pa.array(
        [
            [
                {"key": "description:fr", "value": "Français"},
                {"key": "description", "value": "Base"},
            ]
        ],
        type=pa.list_(pa.struct([pa.field("key", pa.string()), pa.field("value", pa.string())])),
    )
    row = _row(tags.to_pylist()[0])
    before = deepcopy(row)

    extract_description_entries(row)

    assert row == before


def test_arrow_list_scalar_is_read_without_python_conversion() -> None:
    tag_type = pa.struct([pa.field("key", pa.string()), pa.field("value", pa.string())])
    tags = pa.scalar(
        [
            {"key": "description:fr", "value": "Français"},
            {"key": "description", "value": "Base"},
        ],
        type=pa.list_(tag_type),
    )

    entries = extract_description_entries(_row(tags))

    assert [(entry.tag_key, entry.original_text) for entry in entries] == [
        ("description", "Base"),
        ("description:fr", "Français"),
    ]


def test_arrow_list_array_is_read_without_python_conversion() -> None:
    tag_type = pa.struct([pa.field("key", pa.string()), pa.field("value", pa.string())])
    tags = pa.array(
        [
            [
                {"key": "description:fr", "value": "Français"},
                {"key": "description", "value": "Base"},
            ]
        ],
        type=pa.list_(tag_type),
    )

    entries = extract_description_entries(_row(tags))

    assert [(entry.tag_key, entry.original_text) for entry in entries] == [
        ("description", "Base"),
        ("description:fr", "Français"),
    ]


def test_a_multi_row_arrow_column_cannot_be_mistaken_for_one_objects_tags() -> None:
    tags = pa.array(
        [[{"key": "description", "value": "First"}], [{"key": "description", "value": "Second"}]],
        type=pa.list_(pa.struct([pa.field("key", pa.string()), pa.field("value", pa.string())])),
    )

    with pytest.raises(DescriptionRecordError):
        extract_description_entries(_row(tags))


def test_arrow_struct_array_is_read_without_python_conversion() -> None:
    tag_type = pa.struct([pa.field("key", pa.string()), pa.field("value", pa.string())])
    tags = pa.array(
        [
            {"key": "description:fr", "value": "Français"},
            {"key": "description", "value": "Base"},
        ],
        type=tag_type,
    )

    entries = extract_description_entries(_row(tags))

    assert [(entry.tag_key, entry.original_text) for entry in entries] == [
        ("description", "Base"),
        ("description:fr", "Français"),
    ]


def test_arrow_tag_struct_rejects_unexpected_fields() -> None:
    tags = pa.array(
        [[{"key": "description", "value": "Text", "extra": "unexpected"}]],
        type=pa.list_(
            pa.struct(
                [
                    pa.field("key", pa.string()),
                    pa.field("value", pa.string()),
                    pa.field("extra", pa.string()),
                ]
            )
        ),
    )

    with pytest.raises(DescriptionRecordError, match="exactly key and value"):
        extract_description_entries(_row(tags.to_pylist()[0]))


def test_invalid_unicode_surrogate_is_rejected_before_hashing() -> None:
    with pytest.raises(UnicodeEncodeError):
        DescriptionEntry("europe.osm.pbf", "way", 17, "description", "bad\ud800")


@pytest.mark.parametrize(
    "tags",
    [
        [{"key": "description", "value": "one"}, {"key": "description", "value": "two"}],
        [{"key": "description:en", "value": "one"}, {"key": "description:en", "value": "two"}],
    ],
)
def test_duplicate_tag_keys_are_rejected(tags: list[dict[str, str]]) -> None:
    with pytest.raises(DescriptionRecordError, match="duplicate tag key"):
        extract_description_entries(_row(tags))


@pytest.mark.parametrize(
    ("tags", "message"),
    [
        (["not a key/value struct"], "tag entry must be a key/value struct"),
        ([{"value": "missing key"}], "tag struct must contain exactly key and value"),
        ([{"key": "description", "value": 42}], "tag value must be a string or null"),
        ([{"key": 42, "value": "not a string key"}], "tag key must be a non-empty string"),
        ([None], "tag entry must be a key/value struct"),
    ],
)
def test_malformed_arrow_tag_entries_are_rejected(tags: list[object], message: str) -> None:
    with pytest.raises(DescriptionRecordError) as caught:
        extract_description_entries(_row(tags))
    assert str(caught.value) == message


def test_description_identity_excludes_source_but_binds_object_tag_and_text() -> None:
    tags = [{"key": "description", "value": "Repeated text"}]
    first = extract_description_entries(_row(tags, source_pbf="a.osm.pbf", osm_id=1))[0]
    same_identity = extract_description_entries(_row(tags, source_pbf="b.osm.pbf", osm_id=1))[0]
    different_object = extract_description_entries(_row(tags, source_pbf="a.osm.pbf", osm_id=2))[0]
    different_text = extract_description_entries(
        _row([{"key": "description", "value": "Different text"}], osm_id=1)
    )[0]

    assert first.text_sha256 != ""
    assert len(first.text_sha256) == 64
    assert first.description_identity == same_identity.description_identity
    assert first.description_identity != different_object.description_identity
    assert first.description_identity != different_text.description_identity
    assert first.text_sha256 == same_identity.text_sha256


def test_description_identity_has_stable_expected_sha() -> None:
    entry = DescriptionEntry("a.osm.pbf", "way", 1, "description", "Repeated text")

    assert entry.text_sha256 == "fcbe4037c8f97f6d38aa933c437fd883173a97e80ea76112b82d58a3700dce55"
    assert (
        entry.description_identity
        == "5d48fd954edfb06ed837ef847082335c3f94badbd512c4436212a7fbad28cd0c"
    )


def test_description_identity_preserves_unicode_in_opaque_tag_suffixes() -> None:
    entry = extract_description_entries(_row([("description:é", "Café près du parc")]))[0]

    assert entry.text_sha256 == "9d35b838486f78bcff543487c5c33c860cbd4b2ea81f4760bbc6be8759691207"
    assert entry.description_identity == (
        "bc27fb759107e0620a080c7006b72f24718bcdd8040f64b461b72dae01ff3e62"
    )


def test_description_identity_distinguishes_tag_keys() -> None:
    base = DescriptionEntry("a.osm.pbf", "way", 1, "description", "Repeated text")
    localized = DescriptionEntry("a.osm.pbf", "way", 1, "description:en", "Repeated text")

    assert base.text_sha256 == localized.text_sha256
    assert base.description_identity != localized.description_identity


def test_missing_row_metadata_is_rejected() -> None:
    with pytest.raises(DescriptionRecordError, match="source_pbf"):
        extract_description_entries({"osm_type": "way", "osm_id": 1, "tags": []})

    with pytest.raises(DescriptionRecordError, match="^osm_id must be an integer$"):
        extract_description_entries(
            {"source_pbf": "x.osm.pbf", "osm_type": "way", "osm_id": True, "tags": []}
        )


@pytest.mark.parametrize("field", ["source_pbf", "osm_type"])
def test_empty_metadata_is_invalid_even_without_description_tags(field: str) -> None:
    row = {**_row([]), field: ""}

    with pytest.raises(DescriptionRecordError) as caught:
        extract_description_entries(row)

    assert str(caught.value) == f"{field} must be a non-empty string"


class _ArrowScalar:
    """Minimal stand-in for a PyArrow scalar exposing ``as_py``."""

    def __init__(self, value: object) -> None:
        self._value = value

    def as_py(self) -> object:
        return self._value


def test_a_null_arrow_tag_column_yields_no_entries() -> None:
    row = {
        "source_pbf": "region.osm.pbf",
        "osm_type": "way",
        "osm_id": 1,
        "tags": _ArrowScalar(None),
    }

    assert extract_description_entries(row) == ()


def test_a_non_sequence_tag_column_is_rejected() -> None:
    row = {"source_pbf": "region.osm.pbf", "osm_type": "way", "osm_id": 1, "tags": 7}

    with pytest.raises(DescriptionRecordError) as caught:
        extract_description_entries(row)
    assert str(caught.value) == "tags must be an Arrow list of key/value structs"


def test_arrow_scalar_tag_entries_are_unwrapped() -> None:
    row = {
        "source_pbf": "region.osm.pbf",
        "osm_type": "way",
        "osm_id": 1,
        "tags": [_ArrowScalar({"key": "description", "value": "A description"})],
    }

    entries = extract_description_entries(row)

    assert [entry.tag_key for entry in entries] == ["description"]
    assert entries[0].original_text == "A description"


def test_a_pair_shaped_tag_entry_is_accepted() -> None:
    row = {
        "source_pbf": "region.osm.pbf",
        "osm_type": "way",
        "osm_id": 1,
        "tags": [("description", "A description"), ("building", "yes")],
    }

    entries = extract_description_entries(row)

    assert [entry.original_text for entry in entries] == ["A description"]


def test_a_tag_entry_with_the_wrong_arity_is_rejected() -> None:
    row = {
        "source_pbf": "region.osm.pbf",
        "osm_type": "way",
        "osm_id": 1,
        "tags": [("description", "text", "extra")],
    }

    with pytest.raises(DescriptionRecordError) as caught:
        extract_description_entries(row)
    assert str(caught.value) == "tag entry must contain exactly key and value"


def test_a_row_must_be_a_mapping() -> None:
    with pytest.raises(DescriptionRecordError) as caught:
        extract_description_entries([("description", "text")])  # type: ignore[arg-type]
    assert str(caught.value) == "row must be a mapping"


@pytest.mark.parametrize(
    ("kwargs", "message"),
    [
        ({"source_pbf": ""}, "source_pbf must be a non-empty string"),
        ({"osm_type": 7}, "osm_type must be a non-empty string"),
        ({"osm_id": "1"}, "osm_id must be an integer"),
        ({"osm_id": True}, "osm_id must be an integer"),
        ({"tag_key": "name"}, "tag_key must be description or description:<suffix>"),
        ({"tag_key": "description:"}, "tag_key must be description or description:<suffix>"),
        ({"original_text": 7}, "original_text must be a string"),
    ],
)
def test_an_entry_validates_its_fields(kwargs: dict[str, object], message: str) -> None:
    defaults: dict[str, object] = {
        "source_pbf": "region.osm.pbf",
        "osm_type": "way",
        "osm_id": 1,
        "tag_key": "description",
        "original_text": "A description",
    }

    with pytest.raises(DescriptionRecordError) as caught:
        DescriptionEntry(**{**defaults, **kwargs})  # type: ignore[arg-type]
    assert str(caught.value) == message
