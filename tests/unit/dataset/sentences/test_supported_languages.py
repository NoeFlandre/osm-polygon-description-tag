"""The gate that decides which detected languages SaT-3l-sm may split.

SaT is competent on the 85 languages its supervised mixture was trained on. The
detector upstream emits ISO 639-3, SaT names its languages in ISO 639-1, and the
two sets do not line up: a code we cannot map is a code we must not split, so
this table is fail-closed by construction.
"""

from __future__ import annotations

import pytest

from osm_polygon_description_tag.dataset.sentences.languages import (
    SAT_LANGUAGE_COUNT,
    SAT_SUPPORTED_LANGUAGES,
    sat_language_for,
)


def test_the_supported_set_is_the_eighty_five_languages_sat_was_trained_on() -> None:
    """The count is pinned: a model swap that changes it must fail loudly."""
    assert SAT_LANGUAGE_COUNT == 85
    assert len(SAT_SUPPORTED_LANGUAGES) == 85
    assert "en" in SAT_SUPPORTED_LANGUAGES
    assert "ceb" in SAT_SUPPORTED_LANGUAGES


@pytest.mark.parametrize(
    ("detected", "expected"),
    [
        ("eng", "en"),
        ("fra", "fr"),
        ("deu", "de"),
        ("zho", "zh"),
        ("ceb", "ceb"),
        ("nob", "no"),
        ("nno", "no"),
        ("msa", "ms"),
    ],
)
def test_a_detected_iso_639_3_code_maps_to_its_sat_language(detected: str, expected: str) -> None:
    assert sat_language_for(detected) == expected


@pytest.mark.parametrize("detected", ["hrv", "bos", "tgl", "swa", "sot", "xxx", "", "EN"])
def test_a_language_sat_was_not_trained_on_maps_to_nothing(detected: str) -> None:
    """Fail closed: an unmapped code is never guessed at."""
    assert sat_language_for(detected) is None


def test_a_table_that_claims_one_code_for_two_languages_is_refused() -> None:
    """The inversion is only sound while every 639-3 code has one owner."""
    from osm_polygon_description_tag.dataset.sentences.languages import _inverted

    with pytest.raises(ValueError) as caught:
        _inverted({"en": ("eng",), "fr": ("eng",)})

    assert str(caught.value) == "ISO 639-3 code claimed twice: eng"


def test_the_inversion_maps_every_alias_to_its_own_language() -> None:
    from osm_polygon_description_tag.dataset.sentences.languages import _inverted

    assert _inverted({"no": ("nor", "nob", "nno")}) == {
        "nor": "no",
        "nob": "no",
        "nno": "no",
    }
