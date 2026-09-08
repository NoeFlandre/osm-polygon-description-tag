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
    with pytest.raises(LanguagePublicationError, match="different configuration"):
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

    with pytest.raises(LanguagePublicationError, match="complete lines"):
        install_language_card(card, export)
