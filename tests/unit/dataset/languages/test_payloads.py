"""Persisted JSON must retain types and identify malformed fields precisely."""

import pytest

from osm_polygon_description_tag.dataset.languages.payloads import require_object


class InvalidState(ValueError):
    pass


def test_missing_nested_fields_keep_the_callers_error_and_context() -> None:
    reader = require_object({"nested": {}}, error=InvalidState, label="checkpoint")

    with pytest.raises(InvalidState) as caught:
        reader.reader("nested").raw("cursor")

    assert str(caught.value) == "checkpoint payload is missing cursor"


@pytest.mark.parametrize(
    ("method", "value", "diagnostic"),
    [
        ("text", 1, "must be a string"),
        ("integer", True, "must be an integer"),
        ("integer", 1.0, "must be an integer"),
        ("number", False, "must be a real number"),
        ("number", "1", "must be a real number"),
        ("items", (), "must be a list"),
        ("texts", ["valid", None], "must contain only strings"),
        ("mapping", [], "must be an object"),
    ],
)
def test_malformed_fields_report_the_field_and_expected_type(
    method: str, value: object, diagnostic: str
) -> None:
    reader = require_object({"position": value}, error=InvalidState, label="receipt")

    with pytest.raises(InvalidState) as caught:
        getattr(reader, method)("position")

    assert str(caught.value) == f"receipt field position {diagnostic}"


def test_decoding_preserves_empty_values_and_order_without_coercion() -> None:
    reader = require_object(
        {"text": "", "cursor": 0, "budget": 2, "names": ["z", "", "a"], "data": {}},
        error=InvalidState,
        label="state",
    )

    assert reader.text("text") == ""
    assert reader.integer("cursor") == 0
    assert reader.number("budget") == 2.0
    assert reader.texts("names") == ("z", "", "a")
    assert reader.mapping("data") == {}
