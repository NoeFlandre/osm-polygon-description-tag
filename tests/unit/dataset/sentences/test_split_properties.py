"""Properties the split gate must hold for every input, not just the examples.

The gate is the only thing standing between a language the splitter has never
seen and a published sentence list, so the invariants below are stated over
generated input rather than a handful of cases.
"""

from __future__ import annotations

from hypothesis import given, settings
from hypothesis import strategies as st

from osm_polygon_description_tag.dataset.languages.models import LanguageResult, LanguageStatus
from osm_polygon_description_tag.dataset.sentences.languages import (
    SAT_SUPPORTED_LANGUAGES,
    sat_language_for,
    supported_languages_fingerprint,
)
from osm_polygon_description_tag.dataset.sentences.models import SentenceSplitStatus
from osm_polygon_description_tag.dataset.sentences.splitter import GatedSentenceSplitter

_SUPPORTED_ISO = ("eng", "fra", "deu", "zho", "ceb", "nob", "nno", "msa", "arb", "cmn")
_UNSUPPORTED_ISO = ("hrv", "bos", "tgl", "swa", "sot", "lug", "mri", "tsn")


class _CountingSplitter:
    def __init__(self) -> None:
        self.calls: list[tuple[str, str]] = []

    def split(self, text: str, *, language: str) -> tuple[str, ...]:
        self.calls.append((text, language))
        return (text,)


@given(st.text())
@settings(max_examples=200)
def test_the_language_lookup_is_total_and_stays_inside_the_supported_set(
    code: str,
) -> None:
    """Any string is answerable, and any answer is a language SaT was trained on."""
    mapped = sat_language_for(code)

    assert mapped is None or mapped in SAT_SUPPORTED_LANGUAGES


@given(st.one_of(st.integers(), st.none(), st.booleans(), st.binary(), st.lists(st.text())))
def test_a_language_code_that_is_not_text_maps_to_nothing(code: object) -> None:
    assert sat_language_for(code) is None


@given(
    text=st.text(),
    status=st.sampled_from([LanguageStatus.UNCERTAIN, LanguageStatus.NON_LINGUISTIC]),
)
def test_an_unsettled_detection_never_reaches_the_splitter(
    text: str, status: LanguageStatus
) -> None:
    splitter = _CountingSplitter()
    detection = LanguageResult(None, None, None, None, status, "reason")

    result = GatedSentenceSplitter(splitter).split_for(text, detection)

    assert result.status is SentenceSplitStatus.NOT_DETECTED
    assert result.sentences == ()
    assert splitter.calls == []


@given(text=st.text(), code=st.sampled_from(_UNSUPPORTED_ISO))
def test_an_unsupported_language_never_reaches_the_splitter(text: str, code: str) -> None:
    splitter = _CountingSplitter()
    detection = LanguageResult(code, 0.9, 0.1, 0.8, LanguageStatus.DETECTED, "detected")

    result = GatedSentenceSplitter(splitter).split_for(text, detection)

    assert result.status is SentenceSplitStatus.UNSUPPORTED_LANGUAGE
    assert result.reason == f"unsupported_language_{code}"
    assert splitter.calls == []


@given(text=st.text(), code=st.sampled_from(_SUPPORTED_ISO))
def test_a_supported_language_reaches_the_splitter_exactly_once(text: str, code: str) -> None:
    splitter = _CountingSplitter()
    detection = LanguageResult(code, 0.9, 0.1, 0.8, LanguageStatus.DETECTED, "detected")

    result = GatedSentenceSplitter(splitter).split_for(text, detection)

    assert result.status is SentenceSplitStatus.SPLIT
    assert [call[0] for call in splitter.calls] == [text]
    assert splitter.calls[0][1] == sat_language_for(code)


@given(
    text=st.text(),
    code=st.sampled_from(_SUPPORTED_ISO + _UNSUPPORTED_ISO),
    settled=st.booleans(),
)
def test_only_a_split_description_ever_carries_sentences(
    text: str, code: str, settled: bool
) -> None:
    """The published invariant: sentences imply the split status, always."""
    status = LanguageStatus.DETECTED if settled else LanguageStatus.UNCERTAIN
    detection = LanguageResult(
        code if settled else None,
        0.9 if settled else None,
        0.1 if settled else None,
        0.8 if settled else None,
        status,
        "reason",
    )

    result = GatedSentenceSplitter(_CountingSplitter()).split_for(text, detection)

    assert not result.sentences or result.status is SentenceSplitStatus.SPLIT


def test_the_supported_set_fingerprint_is_stable_across_calls() -> None:
    """The fingerprint binds a run to this set, so it must not vary run to run."""
    assert supported_languages_fingerprint() == supported_languages_fingerprint()
    assert len(supported_languages_fingerprint()) == 64
