"""Exact text-editing contracts of the dataset-card section installer.

These helpers rewrite a README that already carries unrelated prose and
configuration. Every boundary here decides whether a byte of someone else's
card survives, so each is asserted at its exact offset rather than by shape.
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest
import yaml

from osm_polygon_description_tag.publication import language_card
from osm_polygon_description_tag.publication.language_card import (
    LANGUAGE_CARD_SECTION_END,
    LANGUAGE_CARD_SECTION_START,
    _append_section,
    _append_unmarked_section,
    _config_entry,
    _config_node,
    _construct_unique_mapping,
    _consume_line_ending,
    _flow_separator,
    _front_matter,
    _install_block_config,
    _install_config,
    _install_flow_config,
    _line_prefix,
    _line_suffix,
    _load_front_matter,
    _marker_offsets,
    _parse_config_entries,
    _replace_marked_section,
    _replace_section,
    _require_mapping_front_matter,
    _require_marker_lines,
    _section_block,
    _valid_language_config,
    _validate_marker_counts,
)
from osm_polygon_description_tag.publication.models import PublicationError
from tests.helpers.messages import exactly

_START = LANGUAGE_CARD_SECTION_START
_END = LANGUAGE_CARD_SECTION_END


def test_markers_are_found_at_their_first_occurrence() -> None:
    """A later duplicate must not move the span; the first pair is the section."""
    readme = f"{_START}\na\n{_END}\nlater {_START} and {_END}\n"

    start, end = _marker_offsets(readme)

    assert start == 0
    assert end == readme.index(_END)
    assert readme[start : start + len(_START)] == _START


def test_marker_offset_rejection_message_is_exact() -> None:
    with pytest.raises(
        PublicationError, match=exactly("dataset card has malformed language-v1 section markers")
    ):
        _marker_offsets("no markers")


def test_a_marker_at_offset_zero_is_accepted() -> None:
    """`find` returns 0 for the first byte, which must not read as absent."""
    readme = f"{_START}\nbody\n{_END}\n"

    assert _marker_offsets(readme) == (0, readme.index(_END))


@pytest.mark.parametrize(
    "readme",
    [
        "no markers at all\n",
        f"{_START}\nonly a start\n",
        f"{_END}\nonly an end\n",
        f"{_END}\n{_START}\n",
    ],
)
def test_absent_or_inverted_markers_are_refused_exactly(readme: str) -> None:
    with pytest.raises(PublicationError) as caught:
        _marker_offsets(readme)

    assert "has malformed language-v1 section markers" in str(caught.value)


def test_markers_in_the_same_order_but_adjacent_are_accepted() -> None:
    readme = f"{_START}{_END}"

    start, end = _marker_offsets(readme)

    assert start == 0
    assert end == len(_START)


@pytest.mark.parametrize(
    ("readme", "position", "expected"),
    [
        ("marker", 0, True),
        ("a\nmarker", 2, True),
        ("a\r\nmarker", 3, True),
        ("text marker", 5, False),
        ("a\n  marker", 4, False),
    ],
)
def test_a_marker_only_starts_a_line_when_nothing_precedes_it(
    readme: str, position: int, expected: bool
) -> None:
    assert _line_prefix(readme, position) is expected


@pytest.mark.parametrize(
    ("readme", "position", "expected"),
    [
        ("marker", 6, True),
        ("marker\nrest", 6, True),
        ("marker\r\nrest", 6, True),
        ("marker tail", 6, False),
    ],
)
def test_a_marker_only_ends_a_line_when_nothing_follows_it(
    readme: str, position: int, expected: bool
) -> None:
    assert _line_suffix(readme, position) is expected


@pytest.mark.parametrize(
    ("readme", "position", "expected"),
    [
        ("x\r\nrest", 1, 3),
        ("x\nrest", 1, 2),
        ("xrest", 1, 1),
        ("x", 1, 1),
    ],
)
def test_exactly_one_line_ending_is_consumed(readme: str, position: int, expected: int) -> None:
    """A CRLF is two bytes and an LF is one; consuming the wrong count eats prose."""
    assert _consume_line_ending(readme, position) == expected


@pytest.mark.parametrize(
    ("before", "trimmed", "separator"),
    [
        ("[", "[", ""),
        ("[ ", "[", " "),
        ("[a,", "[a,", ""),
        ("[a, ", "[a,", " "),
        ("[a", "[a", ", "),
    ],
)
def test_the_flow_separator_preserves_whatever_spacing_was_there(
    before: str, trimmed: str, separator: str
) -> None:
    assert _flow_separator(before, trimmed) == separator


def test_a_config_is_appended_to_a_flow_list_without_reordering_its_keys() -> None:
    """`sort_keys=False` is what keeps `config_name` first, as the Hub expects."""
    config = {"config_name": "language-v1", "data_files": [{"split": "train", "path": "x"}]}

    result = _install_flow_config("configs: [{a: 1}", len("configs: [{a: 1}"), config)

    appended = result[len("configs: [{a: 1}") :]
    assert appended.startswith(", ")
    assert appended.index("config_name") < appended.index("data_files")
    assert yaml.safe_load(f"{result}]")["configs"][-1] == config


def test_an_appended_config_keeps_unicode_unescaped_and_stays_on_one_line() -> None:
    """`allow_unicode=True` and a wide `width` keep the flow list a single line."""
    config = {"config_name": "language-v1", "note": "réunion" * 20}

    result = _install_flow_config("configs: [", len("configs: ["), config)

    assert "\n" not in result
    assert "réunion" in result
    assert "\\u" not in result


@pytest.mark.parametrize(
    ("readme", "detail"),
    [
        ("no front matter\n", "must start with YAML front matter"),
        ("---\nconfigs: []\n", "has no closing YAML front matter delimiter"),
        ("---\n---\nbody\n", "front matter is empty"),
    ],
)
def test_front_matter_rejection_messages_are_exact(readme: str, detail: str) -> None:
    with pytest.raises(PublicationError, match=exactly(f"dataset card {detail}")):
        _front_matter(readme)


def test_front_matter_requires_both_yaml_and_node_mappings() -> None:
    mapping_root = yaml.compose("configs: []")
    scalar_root = yaml.compose("item")
    assert mapping_root is not None
    assert scalar_root is not None

    with pytest.raises(
        PublicationError, match=exactly("dataset card front matter must be a mapping")
    ):
        _require_mapping_front_matter([], mapping_root)
    with pytest.raises(
        PublicationError, match=exactly("dataset card front matter must be a mapping")
    ):
        _require_mapping_front_matter({}, scalar_root)


@pytest.mark.parametrize("configs", [None, [], {}, "not-a-list"])
def test_config_entries_require_a_non_empty_list(configs: object) -> None:
    root = yaml.compose("configs: []")
    assert root is not None

    with pytest.raises(
        PublicationError, match=exactly("dataset card must contain a non-empty configs list")
    ):
        _parse_config_entries({"configs": configs}, root)


@pytest.mark.parametrize("document", ["configs: {}", "other: []"])
def test_config_node_requires_a_yaml_sequence(document: str) -> None:
    root = yaml.compose(document)
    assert root is not None

    with pytest.raises(
        PublicationError, match=exactly("dataset card must contain a configs sequence")
    ):
        _config_node(root)


@pytest.mark.parametrize(
    ("entry", "detail"),
    [
        ("not-a-mapping", "configs entries must be mappings"),
        ({"config_name": ""}, "configs entries need a non-empty config_name"),
        ({"config_name": 7}, "configs entries need a non-empty config_name"),
    ],
)
def test_config_entry_rejection_messages_are_exact(entry: object, detail: str) -> None:
    with pytest.raises(PublicationError, match=exactly(f"dataset card {detail}")):
        _config_entry(entry, set())


def test_conflicting_language_config_message_is_exact() -> None:
    with pytest.raises(
        PublicationError,
        match=exactly("dataset card contains a conflicting language-v1 configuration"),
    ):
        _valid_language_config(
            [{"config_name": "language-v1", "data_files": []}],
            {"config_name": "language-v1", "data_files": ["new.parquet"]},
        )


def test_flow_config_preserves_insertion_order() -> None:
    result = _install_flow_config("configs: [", len("configs: ["), {"z": 1, "a": 2})

    assert result == "configs: [{z: 1, a: 2}"


def test_flow_config_serializer_options_are_explicit(monkeypatch: pytest.MonkeyPatch) -> None:
    seen: dict[str, object] = {}

    def safe_dump(value: object, **kwargs: object) -> str:
        seen["value"] = value
        seen["kwargs"] = kwargs
        return "entry"

    monkeypatch.setattr(language_card.yaml, "safe_dump", safe_dump)

    _install_flow_config("configs: [", len("configs: ["), {"config_name": "language-v1"})

    assert seen == {
        "value": {"config_name": "language-v1"},
        "kwargs": {
            "default_flow_style": True,
            "sort_keys": False,
            "width": 1000,
            "allow_unicode": True,
        },
    }


def test_flow_config_uses_the_wide_single_line_serializer() -> None:
    config = {
        "config_name": "language-v1",
        "data_files": [
            {"split": "train", "path": [f"language-v1/{i:03}.parquet" for i in range(10)]}
        ],
    }

    result = _install_flow_config("configs: [", len("configs: ["), config)

    assert "\n" not in result


def test_flow_config_does_not_strip_newlines_or_non_space_suffixes() -> None:
    config = {"config_name": "language-v1"}

    newline_result = _install_flow_config("configs: [item\n", len("configs: [item\n"), config)
    suffix_result = _install_flow_config("configs: [itemXX \t", len("configs: [itemXX \t"), config)

    assert newline_result.startswith("configs: [item\n, ")
    assert suffix_result.startswith("configs: [itemXX \t, ")


def test_an_empty_flow_list_has_no_inserted_leading_space() -> None:
    result = _install_flow_config("configs: [", len("configs: ["), {"config_name": "language-v1"})

    assert result == "configs: [{config_name: language-v1}"


def test_flow_config_distinguishes_the_width_boundary() -> None:
    config = {"note": "a" * 994 + " b"}

    result = _install_flow_config("configs: [", len("configs: ["), config)

    assert "\n" in result


def test_flow_separator_keeps_newline_and_prefix_boundaries() -> None:
    config = {"config_name": "language-v1"}

    newline_result = _install_flow_config("configs: [\n", len("configs: [\n"), config)
    suffix_result = _install_flow_config("configs: [XX \t", len("configs: [XX \t"), config)

    assert newline_result.startswith("configs: [\n, ")
    assert suffix_result.startswith("configs: [XX \t, ")


def test_block_config_preserves_newline_boundaries() -> None:
    assert _install_block_config("before\n", 0, len("before\n"), "entry", "\n") == (
        "before\nentry\n"
    )
    assert _install_block_config("before\r", 0, len("before\r"), "entry", "\r\n") == (
        "before\rentry\r\n"
    )
    assert _install_block_config("", 0, 0, "entry", "\n") == "\nentry\n"
    assert _install_block_config("after", 0, 0, "entry", "\n") == "\nentry\nafter"
    assert _install_block_config("\nafter", 0, 0, "entry", "\n") == "\nentry\nafter"
    assert _install_block_config("\rafter", 0, 0, "entry", "\r\n") == "\r\nentry\rafter"


def test_install_config_serializer_options_and_trimming_are_exact(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    seen: dict[str, object] = {}

    def safe_dump(value: object, **kwargs: object) -> str:
        seen["value"] = value
        seen["kwargs"] = kwargs
        return "entry\n"

    def insert(*args: object) -> str:
        seen["entry_text"] = args[2]
        return "updated"

    monkeypatch.setattr(language_card.yaml, "safe_dump", safe_dump)
    monkeypatch.setattr(language_card, "_insert_config", insert)

    result = _install_config("front", object(), [], SimpleNamespace(files=()), "\n")

    assert result == "updated"
    assert seen == {
        "value": [{"config_name": "language-v1", "data_files": [{"split": "train", "path": []}]}],
        "kwargs": {"default_flow_style": False, "sort_keys": False, "allow_unicode": True},
        "entry_text": "entry",
    }


@pytest.mark.parametrize(
    ("dumped", "expected"),
    [("entry \n", "entry "), ("entryXX\n", "entryXX"), ("  entry\n", "  entry")],
)
def test_install_config_only_removes_dumped_line_endings(
    monkeypatch: pytest.MonkeyPatch, dumped: str, expected: str
) -> None:
    seen: dict[str, object] = {}

    monkeypatch.setattr(language_card.yaml, "safe_dump", lambda *args, **kwargs: dumped)
    monkeypatch.setattr(
        language_card,
        "_insert_config",
        lambda *args: seen.setdefault("entry_text", args[2]) or "updated",
    )

    _install_config("front", object(), [], SimpleNamespace(files=()), "\n")

    assert seen["entry_text"] == expected


def test_unique_mapping_passes_the_requested_deep_flag_to_every_node() -> None:
    key_node = object()
    value_node = object()
    loader = SimpleNamespace(calls=[])

    def construct_object(node: object, **kwargs: object) -> str | int:
        loader.calls.append((node, kwargs))
        return "name" if node is key_node else 1

    loader.construct_object = construct_object
    node = SimpleNamespace(value=[(key_node, value_node)])

    assert _construct_unique_mapping(loader, node) == {"name": 1}
    assert loader.calls == [(key_node, {"deep": False}), (value_node, {"deep": False})]


def test_section_block_preserves_trailing_and_leading_text(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(language_card, "render_language_card_section", lambda export: "\nbody \r\n")

    block = _section_block(object(), "\n")

    assert block == f"{_START}\n\nbody \n{_END}\n"


def test_section_block_translates_line_endings_only_for_non_lf_cards(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        language_card,
        "render_language_card_section",
        lambda export: "body\nsecondXX\r\n",
    )

    block = _section_block(object(), "\r\n")

    assert block == f"{_START}\r\nbody\r\nsecondXX\r\n{_END}\r\n"


def test_append_section_does_not_add_a_separator_to_double_newline_text() -> None:
    assert _append_section("body\n\n", "block", "\n") == "body\n\nblock"


def test_line_prefix_handles_a_newline_at_offset_zero_and_a_nonempty_prefix() -> None:
    assert _line_prefix("\nmarker", 1) is True
    assert _line_prefix("\n marker", 2) is False
    assert _line_prefix("\rmarker", 1) is True


def test_marker_count_rejection_is_exact_and_rejects_two_sections() -> None:
    with pytest.raises(
        PublicationError, match=exactly("dataset card has malformed language-v1 section markers")
    ):
        _validate_marker_counts(2, 2)


def test_unmarked_section_rejection_message_is_exact() -> None:
    with pytest.raises(
        PublicationError,
        match=exactly("dataset card contains an unmarked language-v1 card section"),
    ):
        _append_unmarked_section("readme", "block", "\n", 1)


def test_unique_mapping_reports_the_duplicate_key_in_its_error() -> None:
    key_one = object()
    key_two = object()
    loader = SimpleNamespace(calls=[])

    def construct_object(node: object, **kwargs: object) -> str | int:
        del kwargs
        if node in (key_one, key_two):
            return "same"
        return 1

    loader.construct_object = construct_object
    node = SimpleNamespace(value=[(key_one, object()), (key_two, object())])

    with pytest.raises(yaml.YAMLError, match=exactly("duplicate YAML key: 'same'")):
        _construct_unique_mapping(loader, node)


def test_front_matter_node_composition_uses_the_safe_loader(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[dict[str, object]] = []

    def compose(front_matter: str, **kwargs: object) -> object:
        del front_matter
        calls.append(kwargs)
        return object()

    monkeypatch.setattr(yaml, "compose", compose)

    _load_front_matter("configs: []")

    assert calls == [{"Loader": yaml.SafeLoader}]


def test_line_suffix_handles_a_newline_at_the_first_byte() -> None:
    assert _line_suffix("\nrest", 0) is True


@pytest.mark.parametrize(
    "readme",
    [
        f"prefix {_START}\n{_END}\n",
        f"{_START} trailing\n{_END}\n",
    ],
)
def test_marker_line_rejection_message_is_exact(readme: str) -> None:
    start = readme.index(_START)
    end = readme.index(_END)

    with pytest.raises(
        PublicationError,
        match=exactly("dataset card language-v1 section markers must occupy complete lines"),
    ):
        _require_marker_lines(readme, start, end)


def test_marked_section_requires_an_exact_heading_error() -> None:
    readme = f"{_START}\n## Other\n{_END}\n"

    with pytest.raises(
        PublicationError,
        match=exactly(
            "dataset card language-v1 section markers do not contain the section heading"
        ),
    ):
        _replace_section(readme, "replacement")


def test_marked_section_count_error_is_exact() -> None:
    with pytest.raises(
        PublicationError, match=exactly("dataset card has a malformed language-v1 card section")
    ):
        _replace_marked_section("readme", "replacement", 0)
