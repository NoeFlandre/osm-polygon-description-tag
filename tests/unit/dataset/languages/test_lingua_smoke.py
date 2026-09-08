import pytest
from lingua import Language, LanguageDetectorBuilder

from osm_polygon_description_tag.dataset.languages import (
    LanguageStatus,
    build_lingua_detector,
    detect_multiple_languages_of,
)


def test_pinned_lingua_detects_synthetic_english_text(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("RAYON_NUM_THREADS", "1")
    detector = build_lingua_detector(language_codes=("eng", "fra"))

    result = detector("The old stone bridge crosses the river near the village.")

    assert result.status is LanguageStatus.DETECTED
    assert result.language_code == "eng"
    assert result.top_score is not None
    assert result.top_score >= 0.8
    assert detector.identity.library_version == "2.2.0"
    assert detector.identity.language_scope == ("eng", "fra")


def test_lingua_segmentation_adapter_reports_two_bounded_languages(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("RAYON_NUM_THREADS", "1")
    detector = LanguageDetectorBuilder.from_languages(Language.ENGLISH, Language.FRENCH).build()

    codes = detect_multiple_languages_of(
        detector,
        "The old stone bridge crosses the river. Le vieux pont traverse la rivière.",
    )

    assert {"eng", "fra"} <= set(codes)
