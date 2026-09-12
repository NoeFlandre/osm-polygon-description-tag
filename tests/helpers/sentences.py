"""Test doubles for the sentence splitter.

No test loads the real SaT model: splitting is exercised through a deterministic
stand-in, exactly as language detection is. The stand-in splits on a single
space so a test can predict the published sentences without depending on a
transformer's behaviour.
"""

from __future__ import annotations

from osm_polygon_description_tag.dataset.languages.annotations import DescriptionAnnotation
from osm_polygon_description_tag.dataset.languages.models import LanguageResult
from osm_polygon_description_tag.dataset.languages.records import DescriptionEntry
from osm_polygon_description_tag.dataset.sentences.models import SentenceSplitResult
from osm_polygon_description_tag.dataset.sentences.splitter import GatedSentenceSplitter

REMOTE_SAT_MODEL_PATH = "/home/user/models/sat-3l-sm/model.safetensors"

__all__ = [
    "REMOTE_SAT_MODEL_PATH",
    "FakeSentenceSplitter",
    "annotation_for",
    "fake_splitter",
    "split_for",
]


class FakeSentenceSplitter:
    """Split on sentence-ending punctuation, deterministically and locally."""

    def __init__(self) -> None:
        self.calls: list[tuple[str, str]] = []

    def split(self, text: str, *, language: str) -> tuple[str, ...]:
        self.calls.append((text, language))
        if not text.strip():
            return ()
        pieces = [piece.strip() for piece in text.split(".") if piece.strip()]
        return tuple(f"{piece}." for piece in pieces)


def fake_splitter() -> FakeSentenceSplitter:
    """Return a fresh splitter stand-in."""
    return FakeSentenceSplitter()


def split_for(text: str, detection: LanguageResult) -> SentenceSplitResult:
    """Return what the gated stand-in would publish for one description."""
    return GatedSentenceSplitter(fake_splitter()).split_for(text, detection)


def annotation_for(entry: DescriptionEntry, detection: LanguageResult) -> DescriptionAnnotation:
    """Bundle one entry with its detection and the stand-in's split outcome."""
    return DescriptionAnnotation(entry, detection, split_for(entry.original_text, detection))
