"""The gate that stands between language detection and sentence splitting.

The splitter itself knows nothing about detection, and detection knows nothing
about splitting. This module is the only place the two meet, so the rule ---
split only a confidently detected language the splitter was trained on --- is
stated once and is the only thing that has to be trusted.
"""

from __future__ import annotations

from typing import Protocol

from osm_polygon_description_tag.dataset.languages.models import LanguageResult, LanguageStatus
from osm_polygon_description_tag.dataset.sentences.languages import sat_language_for
from osm_polygon_description_tag.dataset.sentences.models import (
    SentenceSplitResult,
    not_detected_result,
    split_result,
    unsupported_language_result,
)


class SentenceSplitter(Protocol):
    """The narrow splitting surface this gate needs.

    Implemented against SaT in production and faked in tests; no test loads the
    real model.
    """

    def split(self, text: str, *, language: str) -> tuple[str, ...]:
        """Return ``text`` split into sentences, in the named SaT language."""
        ...


class GatedSentenceSplitter:
    """Split only what the pinned splitter is competent on, and record the rest."""

    def __init__(self, splitter: SentenceSplitter) -> None:
        self._splitter = splitter

    def split_for(self, text: str, detection: LanguageResult) -> SentenceSplitResult:
        """Return this description's sentences, or why it has none."""
        if detection.status is not LanguageStatus.DETECTED:
            return not_detected_result(str(detection.status))
        language_code = detection.language_code
        sat_language = sat_language_for(language_code)
        if sat_language is None:
            return unsupported_language_result(str(language_code))
        return split_result(self._splitter.split(text, language=sat_language))


__all__ = ["GatedSentenceSplitter", "SentenceSplitter"]
