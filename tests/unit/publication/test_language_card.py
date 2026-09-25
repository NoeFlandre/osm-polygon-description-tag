"""Offline tests for controlled language configuration/card installation."""

from dataclasses import replace
from pathlib import Path

import pytest
import yaml

from osm_polygon_description_tag.publication.language import (
    LANGUAGE_CONFIG_NAME,
    LANGUAGE_DATA_PREFIX,
    LanguageExport,
    LanguagePublicationError,
    LanguageStats,
    render_language_card_section,
)
from osm_polygon_description_tag.publication.language_card import (
    LANGUAGE_CARD_SECTION_END,
    LANGUAGE_CARD_SECTION_START,
    install_language_card,
)
from tests.helpers.messages import exactly


@pytest.fixture
def export(tmp_path: Path) -> LanguageExport:
    return LanguageExport(
        export_root=tmp_path,
        config_name=LANGUAGE_CONFIG_NAME,
        snapshot_id="snapshot-1",
        model_config_fingerprint="config-1",
        library_name="detector",
        library_version="1.0",
        files=(f"{LANGUAGE_DATA_PREFIX}/region.parquet",),
        stats=LanguageStats(
            annotation_count=12,
            object_count=9,
            base_description_count=8,
            localized_description_count=4,
            detected_count=7,
            uncertain_count=3,
            non_linguistic_count=2,
            distinct_language_count=2,
            top_languages=(("eng", 6), ("fra", 1)),
            split_count=0,
            unsupported_language_count=0,
            unsupported_distinct_count=0,
            top_unsupported_languages=(),
            not_detected_count=0,
            sentence_count=0,
        ),
    )


def _card() -> str:
    return (
        "---\n"
        "pretty_name: Existing dataset\n"
        "license: odbl\n"
        "configs:\n"
        "- config_name: default\n"
        "  default: true\n"
        "  data_files:\n"
        "  - split: train\n"
        "    path: data/*.parquet\n"
        "- config_name: extra\n"
        "  data_files:\n"
        "  - split: test\n"
        "    path: extra/*.parquet\n"
        "metadata:\n"
        "  keep: this exact value\n"
        "---\n\n"
        "# Existing title\n\n"
        "Existing prose and the default dataset remain untouched.\n"
    )


def test_install_adds_only_the_language_config_and_controlled_section(
    export: LanguageExport,
) -> None:
    original = _card()

    updated = install_language_card(original, export)

    assert "pretty_name: Existing dataset" in updated
    assert "default: true" in updated
    assert "path: data/*.parquet" in updated
    assert "path: extra/*.parquet" in updated
    assert "metadata:\n  keep: this exact value" in updated
    assert "Existing prose and the default dataset remain untouched." in updated
    assert updated.count("config_name:") == 3
    assert updated.count(LANGUAGE_CARD_SECTION_START) == 1
    assert updated.count(LANGUAGE_CARD_SECTION_END) == 1
    assert render_language_card_section(export) in updated
    assert f"{LANGUAGE_DATA_PREFIX}/*.parquet" not in updated
    assert f"- {LANGUAGE_DATA_PREFIX}/region.parquet" in updated


def test_install_selects_only_sorted_export_files_for_language_train_config(
    export: LanguageExport,
) -> None:
    export = replace(
        export,
        files=(
            f"{LANGUAGE_DATA_PREFIX}/z-region.parquet",
            f"{LANGUAGE_DATA_PREFIX}/a-region.parquet",
        ),
    )

    updated = install_language_card(_card(), export)
    configs = yaml.safe_load(updated.split("---", 2)[1])["configs"]
    language = next(entry for entry in configs if entry["config_name"] == LANGUAGE_CONFIG_NAME)

    assert language == {
        "config_name": LANGUAGE_CONFIG_NAME,
        "data_files": [
            {
                "split": "train",
                "path": [
                    f"{LANGUAGE_DATA_PREFIX}/a-region.parquet",
                    f"{LANGUAGE_DATA_PREFIX}/z-region.parquet",
                ],
            }
        ],
    }
    assert f"{LANGUAGE_DATA_PREFIX}/stale.parquet" not in language["data_files"][0]["path"]


def test_install_refuses_an_existing_wildcard_language_config(
    export: LanguageExport,
) -> None:
    conflicting = _card().replace(
        "- config_name: extra\n  data_files:\n  - split: test\n    path: extra/*.parquet\n",
        "- config_name: language-v1\n"
        "  data_files:\n"
        "  - split: train\n"
        "    path: language-v1/data/*.parquet\n",
    )

    with pytest.raises(LanguagePublicationError, match="conflicting language-v1"):
        install_language_card(conflicting, export)


def test_install_refuses_an_existing_language_config_with_a_stale_path(
    export: LanguageExport,
) -> None:
    conflicting = _card().replace(
        "- config_name: extra\n  data_files:\n  - split: test\n    path: extra/*.parquet\n",
        "- config_name: language-v1\n"
        "  data_files:\n"
        "  - split: train\n"
        "    path:\n"
        "    - language-v1/data/region.parquet\n"
        "    - language-v1/data/stale.parquet\n",
    )

    with pytest.raises(LanguagePublicationError, match="conflicting language-v1"):
        install_language_card(conflicting, export)


def test_install_is_idempotent_for_its_own_config_and_section(
    export: LanguageExport,
) -> None:
    first = install_language_card(_card(), export)

    assert install_language_card(first, export) == first


def test_install_preserves_an_indented_or_flow_style_configs_list(
    export: LanguageExport,
) -> None:
    cards = (
        "---\n"
        "configs:\n"
        "  - config_name: default\n"
        "    data_files:\n"
        "      - split: train\n"
        "        path: data/*.parquet\n"
        "---\nbody\n",
        "---\n"
        "configs: [{config_name: default, data_files: [{split: train, path: data/*.parquet}]}]\n"
        "---\nbody\n",
        "---\n"
        "configs: [{config_name: default, data_files: [{split: train, path: data/*.parquet}]}, ]\n"
        "---\nbody\n",
    )

    for card in cards:
        updated = install_language_card(card, export)
        front_matter = updated.split("---", 2)[1]
        configs = yaml.safe_load(front_matter)["configs"]
        assert [entry["config_name"] for entry in configs] == ["default", "language-v1"]
        language = configs[-1]
        assert language["data_files"] == [
            {"split": "train", "path": [f"{LANGUAGE_DATA_PREFIX}/region.parquet"]}
        ]


def test_install_refuses_a_conflicting_language_config(export: LanguageExport) -> None:
    conflicting = _card().replace(
        "- config_name: extra\n",
        "- config_name: language-v1\n",
    )

    with pytest.raises(LanguagePublicationError, match="conflicting language-v1"):
        install_language_card(conflicting, export)


@pytest.mark.parametrize(
    "card",
    [
        "# no front matter\n",
        "---\nconfigs: [\n---\nbody\n",
        "---\npretty_name: missing configs\n---\nbody\n",
    ],
)
def test_install_refuses_a_malformed_dataset_card(export: LanguageExport, card: str) -> None:
    with pytest.raises(LanguagePublicationError, match="dataset card"):
        install_language_card(card, export)


def test_install_refuses_duplicate_yaml_keys(export: LanguageExport) -> None:
    card = "---\nconfigs:\n- config_name: default\n  data_files: []\n  data_files: []\n---\nbody\n"

    with pytest.raises(LanguagePublicationError, match="front matter is malformed"):
        install_language_card(card, export)


@pytest.mark.parametrize(
    "card",
    [
        "---\nconfigs:\n- config_name: default\n",
        "---\n---\nbody\n",
        "---\n- item\n---\nbody\n",
        "---\nconfigs: []\n---\nbody\n",
        "---\nconfigs: {}\n---\nbody\n",
        "---\nconfigs:\n- nope\n---\nbody\n",
        "---\nconfigs:\n- config_name:\n  data_files: []\n---\nbody\n",
        "---\nconfigs:\n- config_name: 1\n---\nbody\n",
    ],
)
def test_install_refuses_malformed_config_shapes(export: LanguageExport, card: str) -> None:
    with pytest.raises(LanguagePublicationError, match="dataset card"):
        install_language_card(card, export)


def test_install_refuses_duplicate_configuration_names(export: LanguageExport) -> None:
    card = "---\nconfigs:\n- config_name: default\n- config_name: default\n---\nbody\n"

    with pytest.raises(LanguagePublicationError, match="duplicate configuration"):
        install_language_card(card, export)


def test_install_rejects_an_export_with_a_different_configuration(
    export: LanguageExport,
) -> None:
    with pytest.raises(
        LanguagePublicationError,
        match=exactly("language export declares a different configuration name"),
    ):
        install_language_card(_card(), replace(export, config_name="other"))


def test_install_preserves_crlf_and_replaces_its_marked_section(
    export: LanguageExport,
) -> None:
    original = _card().replace("\n", "\r\n")

    first = install_language_card(original, export)
    second = install_language_card(first, export)

    assert second == first
    assert "\r\n" in first
    assert "\r\r\n" not in first
    configs = yaml.safe_load(first.split("---", 2)[1])["configs"]
    assert configs[-1]["data_files"][0]["path"] == [f"{LANGUAGE_DATA_PREFIX}/region.parquet"]


def test_install_handles_a_marked_section_without_a_trailing_newline(
    export: LanguageExport,
) -> None:
    first = install_language_card(_card(), export).rstrip("\n")

    updated = install_language_card(first, export)

    assert updated.count(LANGUAGE_CARD_SECTION_START) == 1
    assert updated.count(LANGUAGE_CARD_SECTION_END) == 1


@pytest.mark.parametrize("suffix", ["", "\n\n"])
def test_install_uses_a_bounded_separator_for_existing_prose(
    export: LanguageExport, suffix: str
) -> None:
    original = _card().rstrip("\n") + suffix

    updated = install_language_card(original, export)

    assert updated.count(LANGUAGE_CARD_SECTION_START) == 1
    assert updated.count(LANGUAGE_CARD_SECTION_END) == 1


def test_install_refuses_an_unmarked_existing_language_section(
    export: LanguageExport,
) -> None:
    card = _card() + "\n## Language annotations (`language-v1`)\n\nHandwritten text.\n"

    with pytest.raises(LanguagePublicationError, match="language-v1 card section"):
        install_language_card(card, export)


def test_install_refuses_unbalanced_controlled_section_markers(
    export: LanguageExport,
) -> None:
    card = _card() + f"\n{LANGUAGE_CARD_SECTION_START}\n"

    with pytest.raises(LanguagePublicationError, match="malformed language-v1 section markers"):
        install_language_card(card, export)


def test_install_refuses_reversed_controlled_section_markers(
    export: LanguageExport,
) -> None:
    card = (
        _card()
        + f"{LANGUAGE_CARD_SECTION_END}\n{LANGUAGE_CARD_SECTION_START}\n"
        + "## Language annotations (`language-v1`)\n"
    )

    with pytest.raises(LanguagePublicationError, match="malformed language-v1 section markers"):
        install_language_card(card, export)


def test_install_refuses_a_marked_section_without_the_heading(
    export: LanguageExport,
) -> None:
    card = _card() + f"{LANGUAGE_CARD_SECTION_START}\n## Other\n{LANGUAGE_CARD_SECTION_END}\n"

    with pytest.raises(LanguagePublicationError, match="card section"):
        install_language_card(card, export)


def test_install_refuses_a_heading_outside_the_marked_section(
    export: LanguageExport,
) -> None:
    card = (
        _card()
        + "## Language annotations (`language-v1`)\n"
        + f"{LANGUAGE_CARD_SECTION_START}\n## Other\n{LANGUAGE_CARD_SECTION_END}\n"
    )

    with pytest.raises(LanguagePublicationError, match="do not contain"):
        install_language_card(card, export)


def test_install_refuses_markers_with_inline_text(export: LanguageExport) -> None:
    card = (
        _card()
        + f"\n{LANGUAGE_CARD_SECTION_START} trailing\n"
        + "## Language annotations (`language-v1`)\n"
        + f"{LANGUAGE_CARD_SECTION_END}\n"
    )

    with pytest.raises(LanguagePublicationError, match="complete lines"):
        install_language_card(card, export)


def test_install_refuses_a_marker_with_prefix_text(export: LanguageExport) -> None:
    card = (
        _card()
        + f"prefix {LANGUAGE_CARD_SECTION_START}\n"
        + "## Language annotations (`language-v1`)\n"
        + f"{LANGUAGE_CARD_SECTION_END}\n"
    )

    with pytest.raises(
        LanguagePublicationError,
        match=exactly("dataset card language-v1 section markers must occupy complete lines"),
    ):
        install_language_card(card, export)


def _card_with_sections() -> str:
    return (
        _card()
        + "\n## Methodology\n\nThe source pipeline is deterministic.\n"
        + "\n## Limitations\n\nSource text can be incomplete.\n"
        + "\n## License and attribution\n\nODbL.\n"
    )


def test_install_places_language_section_before_general_limitations(
    export: LanguageExport,
) -> None:
    updated = install_language_card(_card_with_sections(), export)

    assert updated.index(LANGUAGE_CARD_SECTION_START) < updated.index("## Limitations")
    assert updated.index("## Limitations") < updated.index("## License and attribution")


def test_install_moves_an_existing_end_section_before_general_limitations(
    export: LanguageExport,
) -> None:
    card = _card_with_sections()
    legacy = (
        card
        + "\n"
        + LANGUAGE_CARD_SECTION_START
        + "\n"
        + render_language_card_section(export)
        + "\n"
        + LANGUAGE_CARD_SECTION_END
        + "\n"
    )

    updated = install_language_card(legacy, export)

    assert updated.index(LANGUAGE_CARD_SECTION_START) < updated.index("## Limitations")
    assert updated.count(LANGUAGE_CARD_SECTION_START) == 1
    assert updated.count(LANGUAGE_CARD_SECTION_END) == 1
    assert install_language_card(updated, export) == updated


# ---------------------------------------------------------------------------
# Exact README -> README contracts of the section installer.
#
# Every boundary below decides whether a byte of someone else's card survives,
# so each case pins the whole resulting README (or the whole refusal message)
# rather than a shape.
# ---------------------------------------------------------------------------

_START = LANGUAGE_CARD_SECTION_START
_END = LANGUAGE_CARD_SECTION_END
_HEADING = f"## Language annotations (`{LANGUAGE_CONFIG_NAME}`)"
_REGION = f"{LANGUAGE_DATA_PREFIX}/region.parquet"
_FLOW_ENTRY = (
    f"{{config_name: {LANGUAGE_CONFIG_NAME}, data_files: [{{split: train, path: [{_REGION}]}}]}}"
)


def _block_entry(indent: str, newline: str = "\n") -> str:
    lines = (
        f"- config_name: {LANGUAGE_CONFIG_NAME}",
        "  data_files:",
        "  - split: train",
        "    path:",
        f"    - {_REGION}",
    )
    return newline.join(f"{indent}{line}" for line in lines)


def _section(export: LanguageExport, newline: str = "\n") -> str:
    body = render_language_card_section(export).rstrip("\n").replace("\n", newline)
    return f"{_START}{newline}{body}{newline}{_END}{newline}"


_BLOCK_FRONT = "---\nconfigs:\n- config_name: default\n"


@pytest.mark.parametrize(
    ("front", "expected_front"),
    [
        pytest.param(
            "configs: [{config_name: default}]\n",
            f"configs: [{{config_name: default}}, {_FLOW_ENTRY}]\n",
            id="flow",
        ),
        pytest.param(
            "configs: [{config_name: default}, ]\n",
            f"configs: [{{config_name: default}},  {_FLOW_ENTRY}]\n",
            id="flow-trailing-comma-keeps-its-space",
        ),
        pytest.param(
            "configs: [{config_name: default},]\n",
            f"configs: [{{config_name: default}},{_FLOW_ENTRY}]\n",
            id="flow-trailing-comma-without-space",
        ),
        pytest.param(
            "configs: [ {config_name: default} ]\n",
            f"configs: [ {{config_name: default}} , {_FLOW_ENTRY}]\n",
            id="flow-padded",
        ),
        pytest.param(
            "configs: [{config_name: default},\n  ]\n",
            f"configs: [{{config_name: default}},\n  , {_FLOW_ENTRY}]\n",
            id="flow-multiline-keeps-its-newline",
        ),
        pytest.param(
            "configs:\n- config_name: default\n",
            f"configs:\n- config_name: default\n{_block_entry('')}\n",
            id="block-at-end",
        ),
        pytest.param(
            "configs:\n  - config_name: default\nother: 1\n",
            f"configs:\n  - config_name: default\n{_block_entry('  ')}\nother: 1\n",
            id="block-indented-before-another-key",
        ),
    ],
)
def test_the_language_config_is_appended_without_touching_existing_yaml(
    export: LanguageExport, front: str, expected_front: str
) -> None:
    readme = f"---\n{front}---\n# T\n"

    assert install_language_card(readme, export) == (
        f"---\n{expected_front}---\n# T\n\n{_section(export)}"
    )


def test_a_crlf_card_keeps_crlf_in_its_config_and_its_section(export: LanguageExport) -> None:
    readme = "---\r\nconfigs:\r\n- config_name: default\r\n---\r\n# T\r\n"

    assert install_language_card(readme, export) == (
        "---\r\nconfigs:\r\n- config_name: default\r\n"
        f"{_block_entry('', '\r\n')}\r\n---\r\n# T\r\n\r\n{_section(export, '\r\n')}"
    )


def test_the_appended_flow_config_stays_on_one_line_and_keeps_unicode(
    export: LanguageExport,
) -> None:
    files = tuple(f"{LANGUAGE_DATA_PREFIX}/réunion-{i:03}.parquet" for i in range(10))
    readme = "---\nconfigs: [{config_name: default}]\n---\n# T\n"

    updated = install_language_card(readme, replace(export, files=files))

    config_line = updated.splitlines()[1]
    assert config_line == (
        f"configs: [{{config_name: default}}, {{config_name: {LANGUAGE_CONFIG_NAME}, "
        f"data_files: [{{split: train, path: [{', '.join(files)}]}}]}}]"
    )


def test_a_long_flow_value_is_still_wrapped_past_the_serializer_width(
    export: LanguageExport,
) -> None:
    """The width is wide, not unbounded: one very long line still wraps."""
    files = tuple(f"{LANGUAGE_DATA_PREFIX}/{'a' * 60}-{i:03}.parquet" for i in range(20))
    readme = "---\nconfigs: [{config_name: default}]\n---\n# T\n"

    updated = install_language_card(readme, replace(export, files=files))

    front = updated.split("---\n", 2)[1]
    assert front.count("\n") > 1
    assert yaml.safe_load(front)["configs"][-1]["data_files"][0]["path"] == list(files)


def test_an_appended_block_config_keeps_unicode_unescaped(export: LanguageExport) -> None:
    files = (f"{LANGUAGE_DATA_PREFIX}/réunion.parquet",)

    updated = install_language_card(_BLOCK_FRONT + "---\n# T\n", replace(export, files=files))

    assert f"    - {LANGUAGE_DATA_PREFIX}/réunion.parquet\n" in updated
    assert "\\u" not in updated


@pytest.mark.parametrize(
    ("body", "expected_body"),
    [
        pytest.param("intro", "intro\n\n{section}", id="no-trailing-newline"),
        pytest.param("intro\n", "intro\n\n{section}", id="one-trailing-newline"),
        pytest.param("intro\n\n", "intro\n\n{section}", id="blank-line-already-there"),
        pytest.param(
            "introXX\n\n## Limitations\nbody\n",
            "introXX\n\n{section}\n## Limitations\nbody\n",
            id="before-limitations",
        ),
        pytest.param(
            "introXX  \n\n\n## Limitations\nbody\n",
            "introXX  \n\n{section}\n## Limitations\nbody\n",
            id="hard-line-break-before-limitations-survives",
        ),
    ],
)
def test_the_section_is_placed_with_exactly_one_blank_line_before_it(
    export: LanguageExport, body: str, expected_body: str
) -> None:
    updated = install_language_card(_BLOCK_FRONT + "---\n" + body, export)

    assert updated.split("---\n", 2)[2] == expected_body.format(section=_section(export))


@pytest.mark.parametrize(
    ("old_section", "trailing"),
    [
        pytest.param(f"{_START}\n{_HEADING}\nold\n{_END}\n", "tail\n", id="lf"),
        pytest.param(f"{_START}\n{_HEADING}\nold\n{_END}", "", id="end-of-file"),
        pytest.param(f"{_START}\r\n{_HEADING}\r\nold\r\n{_END}\r\n", "tail\n", id="crlf-markers"),
    ],
)
def test_an_existing_marked_section_is_replaced_in_place(
    export: LanguageExport, old_section: str, trailing: str
) -> None:
    front = f"---\nconfigs:\n- config_name: default\n{_block_entry('')}\n---\n"
    readme = f"{front}intro\n\n{old_section}{trailing}"

    updated = install_language_card(readme, export)

    assert updated == f"{front}intro\n\n{trailing}" + (
        f"\n{_section(export)}" if trailing else _section(export)
    )


@pytest.mark.parametrize(
    ("readme", "message"),
    [
        ("no front matter\n", "dataset card must start with YAML front matter"),
        ("---\nconfigs: []\n", "dataset card has no closing YAML front matter delimiter"),
        ("---\n  \n---\nbody\n", "dataset card front matter is empty"),
        ("---\n- item\n---\nbody\n", "dataset card front matter must be a mapping"),
        ("---\nitem\n---\nbody\n", "dataset card front matter must be a mapping"),
        ("---\nother: []\n---\nbody\n", "dataset card must contain a configs sequence"),
        ("---\nconfigs: {}\n---\nbody\n", "dataset card must contain a configs sequence"),
        ("---\nconfigs: []\n---\nbody\n", "dataset card must contain a non-empty configs list"),
        ("---\nconfigs:\n- nope\n---\nbody\n", "dataset card configs entries must be mappings"),
        (
            "---\nconfigs:\n- config_name: ''\n---\nbody\n",
            "dataset card configs entries need a non-empty config_name",
        ),
        (
            "---\nconfigs:\n- config_name: 7\n---\nbody\n",
            "dataset card configs entries need a non-empty config_name",
        ),
        (
            "---\nconfigs:\n- config_name: a\n- config_name: a\n---\nbody\n",
            "dataset card contains duplicate configuration 'a'",
        ),
        (
            f"---\nconfigs:\n- config_name: {LANGUAGE_CONFIG_NAME}\n  data_files: []\n---\nbody\n",
            "dataset card contains a conflicting language-v1 configuration",
        ),
        (
            "---\nconfigs:\n- config_name: a\n  same: 1\n  same: 2\n---\nbody\n",
            "dataset card front matter is malformed: duplicate YAML key: 'same'",
        ),
    ],
)
def test_a_malformed_front_matter_is_refused_with_an_exact_message(
    export: LanguageExport, readme: str, message: str
) -> None:
    with pytest.raises(LanguagePublicationError, match=exactly(message)):
        install_language_card(readme, export)


_MARKERS = "dataset card has malformed language-v1 section markers"
_LINES = "dataset card language-v1 section markers must occupy complete lines"


@pytest.mark.parametrize(
    ("body", "message"),
    [
        pytest.param(f"{_START}\n{_HEADING}\n", _MARKERS, id="only-start"),
        pytest.param(f"{_HEADING}\n{_END}\n", _MARKERS, id="only-end"),
        pytest.param(f"{_END}\n{_HEADING}\n{_START}\n", _MARKERS, id="inverted"),
        pytest.param(
            f"{_START}\n{_HEADING}\n{_END}\n{_START}\n{_END}\n", _MARKERS, id="two-sections"
        ),
        pytest.param(f"prefix {_START}\n{_HEADING}\n{_END}\n", _LINES, id="start-prefix"),
        pytest.param(f"{_START} trailing\n{_HEADING}\n{_END}\n", _LINES, id="start-suffix"),
        pytest.param(f"{_START}\n{_HEADING}\n {_END}\n", _LINES, id="end-prefix"),
        pytest.param(f"{_START}\n{_HEADING}\n{_END} tail\n", _LINES, id="end-suffix"),
        pytest.param(
            f"{_START}\n## Other\n{_END}\n",
            "dataset card has a malformed language-v1 card section",
            id="no-heading-inside",
        ),
        pytest.param(
            f"{_HEADING}\n{_START}\n## Other\n{_END}\n",
            "dataset card language-v1 section markers do not contain the section heading",
            id="heading-outside",
        ),
        pytest.param(
            f"{_HEADING}\n{_START}\n{_HEADING}\n{_END}\n",
            "dataset card has a malformed language-v1 card section",
            id="heading-twice",
        ),
        pytest.param(
            f"{_HEADING}\nHandwritten.\n",
            "dataset card contains an unmarked language-v1 card section",
            id="unmarked-heading",
        ),
    ],
)
def test_a_malformed_generated_section_is_refused_with_an_exact_message(
    export: LanguageExport, body: str, message: str
) -> None:
    readme = f"{_BLOCK_FRONT}---\nintro\n{body}"

    with pytest.raises(LanguagePublicationError, match=exactly(message)):
        install_language_card(readme, export)


def test_markers_at_the_start_of_crlf_lines_are_accepted(export: LanguageExport) -> None:
    readme = f"{_BLOCK_FRONT}---\nintro\r\n{_START}\r\n{_HEADING}\r\n{_END}\r\n"

    updated = install_language_card(readme, export)

    assert updated.count(_START) == 1
    assert updated.endswith(_section(export))


# ---------------------------------------------------------------------------
# Rendered section content.
# ---------------------------------------------------------------------------


def _stats(**overrides: object) -> LanguageStats:
    base: dict[str, object] = dict(
        annotation_count=100,
        object_count=90,
        base_description_count=80,
        localized_description_count=20,
        detected_count=60,
        uncertain_count=30,
        non_linguistic_count=10,
        distinct_language_count=5,
        top_languages=(("eng", 273435), ("deu", 138912)),
        split_count=45,
        unsupported_language_count=15,
        unsupported_distinct_count=3,
        top_unsupported_languages=(("tso", 10), ("vec", 5)),
        not_detected_count=25,
        sentence_count=70,
    )
    base.update(overrides)
    return LanguageStats(**base)  # type: ignore[arg-type]


def test_the_card_states_that_detection_is_not_gated_on_confidence(
    export: LanguageExport,
) -> None:
    """The card must not describe a policy the run no longer applies."""
    section = render_language_card_section(export)

    assert "meets its confidence policy" not in section
    assert "Detection is **not** gated on a confidence threshold" in section


@pytest.mark.parametrize(
    ("split", "unsupported", "coverage", "share"),
    [
        (45, 15, "75.0000%", "25.0000%"),
        (840897, 44843, "94.9372%", "5.0628%"),
        (1, 0, "100.0000%", "0.0000%"),
        (0, 1, "0.0000%", "100.0000%"),
        (0, 0, "n/a", "n/a"),
    ],
)
def test_coverage_is_a_share_of_eligible_units_only(
    export: LanguageExport, split: int, unsupported: int, coverage: str, share: str
) -> None:
    """Values with no detected language were never candidates for splitting."""
    stats = _stats(split_count=split, unsupported_language_count=unsupported)

    section = render_language_card_section(replace(export, stats=stats))

    assert f"| Eligible text units | {split + unsupported} |\n" in section
    assert f"| Coverage | {coverage} |\n| Unsupported | {share} |\n" in section


def test_the_language_tables_list_every_published_language_in_order(
    export: LanguageExport,
) -> None:
    section = render_language_card_section(replace(export, stats=_stats()))

    assert (
        "\n\nLargest groups left unsplit:\n\n"
        "| Language | Annotations |\n| --- | ---: |\n| `tso` | 10 |\n| `vec` | 5 |\n\n"
    ) in section
    assert (
        "**Most frequent detected languages.**\n\n"
        "| Language | Annotations |\n| --- | ---: |\n| `eng` | 273435 |\n| `deu` | 138912 |\n\n"
    ) in section


def test_empty_language_tables_say_so_rather_than_rendering_a_header(
    export: LanguageExport,
) -> None:
    stats = _stats(top_languages=(), top_unsupported_languages=(), unsupported_language_count=0)

    section = render_language_card_section(replace(export, stats=stats))

    assert "\n\nEvery detected language was inside the supported set.\n\n" in section
    assert "**Most frequent detected languages.**\n\nNo language was detected in this run.\n" in (
        section
    )
    assert "| Language | Annotations |" not in section
