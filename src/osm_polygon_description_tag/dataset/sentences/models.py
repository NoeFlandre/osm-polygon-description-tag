"""What splitting decided for one description, and why.

Every description that reaches the splitter leaves with one of three outcomes,
and each one is published. A row that carries no sentences says which of the
two refusals produced it, so "the splitter does not know this language" is never
confused with "detection never settled on a language".
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from enum import StrEnum
from typing import Final

SPLIT_REASON: Final = "split_sat_3l_sm"
UNSUPPORTED_LANGUAGE_REASON_PREFIX: Final = "unsupported_language_"
NOT_DETECTED_REASON_PREFIX: Final = "not_detected_"


class SentenceSplitStatus(StrEnum):
    """Outcome category for one description's sentence splitting."""

    SPLIT = "split"
    UNSUPPORTED_LANGUAGE = "unsupported_language"
    NOT_DETECTED = "not_detected"


@dataclass(frozen=True, slots=True)
class SentenceSplitResult:
    """One description's sentences, or the recorded reason there are none."""

    status: SentenceSplitStatus
    reason: str
    sentences: tuple[str, ...]

    def __post_init__(self) -> None:
        if not isinstance(self.status, SentenceSplitStatus):
            raise TypeError("status must be a SentenceSplitStatus")
        if not isinstance(self.reason, str) or not self.reason:
            raise ValueError("reason must be a non-empty string")
        _validate_sentences(self.status, self.sentences)


def _validate_sentences(status: SentenceSplitStatus, sentences: object) -> None:
    _require_text_tuple(sentences)
    if status is not SentenceSplitStatus.SPLIT and sentences:
        raise ValueError("only a split description may carry sentences")


def _require_text_tuple(sentences: object) -> None:
    if not isinstance(sentences, tuple):
        raise TypeError("sentences must be a tuple")
    if any(not isinstance(sentence, str) for sentence in sentences):
        raise TypeError("every sentence must be a string")


def split_result(sentences: Sequence[str]) -> SentenceSplitResult:
    """Return the outcome of a description the splitter actually handled."""
    return SentenceSplitResult(SentenceSplitStatus.SPLIT, SPLIT_REASON, tuple(sentences))


def unsupported_language_result(language_code: str) -> SentenceSplitResult:
    """Return the outcome for a language the splitter was not trained on."""
    return SentenceSplitResult(
        SentenceSplitStatus.UNSUPPORTED_LANGUAGE,
        f"{UNSUPPORTED_LANGUAGE_REASON_PREFIX}{language_code}",
        (),
    )


def not_detected_result(detection_status: str) -> SentenceSplitResult:
    """Return the outcome for a description detection never settled on."""
    return SentenceSplitResult(
        SentenceSplitStatus.NOT_DETECTED,
        f"{NOT_DETECTED_REASON_PREFIX}{detection_status}",
        (),
    )


__all__ = [
    "NOT_DETECTED_REASON_PREFIX",
    "SPLIT_REASON",
    "UNSUPPORTED_LANGUAGE_REASON_PREFIX",
    "SentenceSplitResult",
    "SentenceSplitStatus",
    "not_detected_result",
    "split_result",
    "unsupported_language_result",
]
