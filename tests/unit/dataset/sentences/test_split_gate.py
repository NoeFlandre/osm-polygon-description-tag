"""The gate between detection and splitting.

A description is split only when detection actually settled on a language *and*
the splitter was trained on it. Every other outcome is recorded, not silently
dropped: the published row says why it carries no sentences.
"""

from __future__ import annotations

import pytest

from osm_polygon_description_tag.dataset.languages.models import LanguageResult, LanguageStatus
from osm_polygon_description_tag.dataset.sentences.models import (
    SentenceSplitResult,
    SentenceSplitStatus,
)
from osm_polygon_description_tag.dataset.sentences.splitter import GatedSentenceSplitter


class _RecordingSplitter:
    """A stand-in that records exactly what it was asked to split."""

    def __init__(self, sentences: tuple[str, ...] = ("one.", "two.")) -> None:
        self._sentences = sentences
        self.calls: list[tuple[str, str]] = []

    def split(self, text: str, *, language: str) -> tuple[str, ...]:
        self.calls.append((text, language))
        return self._sentences


def _detected(code: str = "eng") -> LanguageResult:
    return LanguageResult(code, 0.9, 0.1, 0.8, LanguageStatus.DETECTED, "detected")


def _uncertain() -> LanguageResult:
    return LanguageResult(None, None, None, None, LanguageStatus.UNCERTAIN, "tie")


def _non_linguistic() -> LanguageResult:
    return LanguageResult(None, None, None, None, LanguageStatus.NON_LINGUISTIC, "too_short")


def test_a_supported_detected_language_is_split_in_that_language() -> None:
    splitter = _RecordingSplitter()
    gated = GatedSentenceSplitter(splitter)

    result = gated.split_for("One. Two.", _detected("fra"))

    assert result == SentenceSplitResult(
        SentenceSplitStatus.SPLIT, "split_sat_3l_sm", ("one.", "two.")
    )
    assert splitter.calls == [("One. Two.", "fr")]


def test_a_detected_language_the_splitter_does_not_support_is_left_unsplit() -> None:
    """Croatian is detected confidently and is still not one of SaT's 85."""
    splitter = _RecordingSplitter()
    gated = GatedSentenceSplitter(splitter)

    result = gated.split_for("Jedan. Dva.", _detected("hrv"))

    assert result == SentenceSplitResult(
        SentenceSplitStatus.UNSUPPORTED_LANGUAGE, "unsupported_language_hrv", ()
    )
    assert splitter.calls == []


@pytest.mark.parametrize(
    ("result", "reason"),
    [
        (_uncertain(), "not_detected_uncertain"),
        (_non_linguistic(), "not_detected_non_linguistic"),
    ],
)
def test_a_description_without_a_settled_language_is_left_unsplit(
    result: LanguageResult, reason: str
) -> None:
    splitter = _RecordingSplitter()
    gated = GatedSentenceSplitter(splitter)

    assert gated.split_for("whatever", result) == SentenceSplitResult(
        SentenceSplitStatus.NOT_DETECTED, reason, ()
    )
    assert splitter.calls == []


def test_a_supported_language_that_yields_no_sentences_is_still_a_split() -> None:
    """An empty split is a real answer; it must not be confused with a refusal."""
    gated = GatedSentenceSplitter(_RecordingSplitter(sentences=()))

    result = gated.split_for("   ", _detected("eng"))

    assert result.status is SentenceSplitStatus.SPLIT
    assert result.sentences == ()


@pytest.mark.parametrize(
    ("sentences", "error", "message"),
    [
        (["one"], TypeError, "sentences must be a tuple"),
        ((7,), TypeError, "every sentence must be a string"),
        (("a", None), TypeError, "every sentence must be a string"),
    ],
)
def test_a_result_whose_sentences_are_not_text_is_refused(
    sentences: object, error: type[Exception], message: str
) -> None:
    with pytest.raises(error) as caught:
        SentenceSplitResult(SentenceSplitStatus.SPLIT, "split_sat_3l_sm", sentences)  # type: ignore[arg-type]

    assert str(caught.value) == message


@pytest.mark.parametrize(
    "status", [SentenceSplitStatus.UNSUPPORTED_LANGUAGE, SentenceSplitStatus.NOT_DETECTED]
)
def test_only_a_split_result_may_carry_sentences(status: SentenceSplitStatus) -> None:
    with pytest.raises(ValueError) as caught:
        SentenceSplitResult(status, "some_reason", ("smuggled",))

    assert str(caught.value) == "only a split description may carry sentences"


def test_a_result_without_a_status_of_the_right_type_is_refused() -> None:
    with pytest.raises(TypeError) as caught:
        SentenceSplitResult("split", "split_sat_3l_sm", ())  # type: ignore[arg-type]

    assert str(caught.value) == "status must be a SentenceSplitStatus"


@pytest.mark.parametrize("reason", ["", None, 7])
def test_a_result_without_a_reason_is_refused(reason: object) -> None:
    with pytest.raises(ValueError) as caught:
        SentenceSplitResult(SentenceSplitStatus.SPLIT, reason, ())  # type: ignore[arg-type]

    assert str(caught.value) == "reason must be a non-empty string"
