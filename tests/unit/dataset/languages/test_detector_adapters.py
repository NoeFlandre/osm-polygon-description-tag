"""Failure paths of the Lingua adapter and the pinned-version guard."""

import importlib.metadata as metadata
from types import SimpleNamespace

import pytest

from osm_polygon_description_tag.dataset.languages import detector as detector_module
from osm_polygon_description_tag.dataset.languages.detector import (
    LanguageDetectionError,
    LanguageDetector,
    LanguageLibraryVersionError,
    build_lingua_detector,
    compute_language_confidence_values,
    detect_multiple_languages_of,
)
from osm_polygon_description_tag.dataset.languages.models import (
    DEFAULT_LANGUAGE_POLICY,
    LanguagePolicy,
    LanguageStatus,
)

TEXT = "A sufficiently long English sentence"


def _language(code: str | None) -> object:
    iso = None if code is None else SimpleNamespace(name=code)
    return SimpleNamespace(iso_code_639_3=iso)


def _confidence(code: str, value: float) -> object:
    return SimpleNamespace(language=_language(code), value=value)


def test_the_policy_is_exposed_on_the_detector() -> None:
    policy = LanguagePolicy(min_alphabetic_chars=7)
    detector = LanguageDetector(lambda text: {"eng": 0.9}, policy=policy)

    assert detector.policy == policy
    assert detector.identity.policy == policy


def test_a_non_string_text_is_rejected() -> None:
    detector = LanguageDetector(lambda text: {"eng": 0.9})

    with pytest.raises(TypeError) as caught:
        detector(7)  # type: ignore[arg-type]
    assert str(caught.value) == "text must be a string"


def test_a_provider_that_does_not_return_a_mapping_is_rejected() -> None:
    detector = LanguageDetector(lambda text: [("eng", 0.9)])  # type: ignore[arg-type,return-value]

    with pytest.raises(LanguageDetectionError) as caught:
        detector(TEXT)
    assert str(caught.value) == "confidence provider must return a mapping"


def test_codes_that_collapse_to_one_are_rejected() -> None:
    detector = LanguageDetector(lambda text: {"eng": 0.9, "ENG": 0.1})

    with pytest.raises(LanguageDetectionError, match="duplicate language code: eng"):
        detector(TEXT)


@pytest.mark.parametrize(
    ("code", "message"),
    [
        (7, "language code must be a string"),
        ("en", "language code must be ISO 639-3"),
        ("engl", "language code must be ISO 639-3"),
        ("1ab", "language code must be ISO 639-3"),
    ],
)
def test_a_malformed_provider_code_is_rejected(code: object, message: str) -> None:
    detector = LanguageDetector(lambda text: {code: 0.9})  # type: ignore[dict-item]

    with pytest.raises(LanguageDetectionError) as caught:
        detector(TEXT)
    assert str(caught.value) == message


def test_segmentation_is_skipped_for_text_that_is_too_short() -> None:
    calls: list[str] = []

    def _segments(text: str) -> tuple[str, ...]:
        calls.append(text)
        return ("eng", "fra")

    detector = LanguageDetector(
        lambda text: {"eng": 0.95, "fra": 0.05},
        policy=LanguagePolicy(min_alphabetic_chars=5),
        multiple_language_codes=_segments,
    )

    result = detector("Hello")

    assert calls == []
    assert result.status is LanguageStatus.DETECTED


def test_segmentation_marks_long_mixed_text_as_uncertain() -> None:
    detector = LanguageDetector(
        lambda text: {"eng": 0.95, "fra": 0.05},
        multiple_language_codes=lambda text: ("eng", "fra"),
    )

    result = detector("This English sentence contient aussi du francais")

    assert result.status is LanguageStatus.UNCERTAIN
    assert result.reason == "mixed_text"


def test_the_confidence_adapter_requires_the_lingua_method() -> None:
    with pytest.raises(LanguageDetectionError) as caught:
        compute_language_confidence_values(object(), TEXT)
    assert str(caught.value) == "Lingua detector lacks confidence-value computation"


def test_the_confidence_adapter_requires_complete_items() -> None:
    engine = SimpleNamespace(
        compute_language_confidence_values=lambda text: [SimpleNamespace(language=None, value=None)]
    )

    with pytest.raises(LanguageDetectionError) as caught:
        compute_language_confidence_values(engine, TEXT)
    assert str(caught.value) == "Lingua confidence item must contain language and value"


@pytest.mark.parametrize(
    "item",
    [
        SimpleNamespace(value=0.9),
        SimpleNamespace(language=_language("ENG")),
    ],
)
def test_the_confidence_adapter_rejects_items_missing_a_required_attribute(
    item: object,
) -> None:
    engine = SimpleNamespace(
        compute_language_confidence_values=lambda text, item=item: [item],
    )

    with pytest.raises(LanguageDetectionError) as caught:
        compute_language_confidence_values(engine, TEXT)
    assert str(caught.value) == "Lingua confidence item must contain language and value"


def test_the_confidence_adapter_requires_an_iterable() -> None:
    engine = SimpleNamespace(compute_language_confidence_values=lambda text: 7)

    with pytest.raises(LanguageDetectionError) as caught:
        compute_language_confidence_values(engine, TEXT)
    assert str(caught.value) == "Lingua confidence values must be iterable"


def test_the_confidence_adapter_rejects_duplicate_codes() -> None:
    engine = SimpleNamespace(
        compute_language_confidence_values=lambda text: [
            _confidence("ENG", 0.9),
            _confidence("eng", 0.1),
        ]
    )

    with pytest.raises(LanguageDetectionError, match="duplicate language code: eng"):
        compute_language_confidence_values(engine, TEXT)


def test_the_confidence_adapter_requires_an_iso_code() -> None:
    engine = SimpleNamespace(
        compute_language_confidence_values=lambda text: [
            SimpleNamespace(language=_language(None), value=0.9)
        ]
    )

    with pytest.raises(LanguageDetectionError) as caught:
        compute_language_confidence_values(engine, TEXT)
    assert str(caught.value) == "Lingua language lacks an ISO 639-3 code"


@pytest.mark.parametrize(
    "language",
    [SimpleNamespace(), SimpleNamespace(iso_code_639_3=SimpleNamespace())],
)
def test_the_confidence_adapter_rejects_missing_iso_code_attributes(language: object) -> None:
    engine = SimpleNamespace(
        compute_language_confidence_values=lambda text, language=language: [
            SimpleNamespace(language=language, value=0.9)
        ]
    )

    with pytest.raises(LanguageDetectionError) as caught:
        compute_language_confidence_values(engine, TEXT)
    assert str(caught.value) == "Lingua language lacks an ISO 639-3 code"


def test_the_confidence_adapter_converts_lingua_items() -> None:
    engine = SimpleNamespace(
        compute_language_confidence_values=lambda text: [
            _confidence("ENG", 0.9),
            _confidence("FRA", 0.1),
        ]
    )

    assert compute_language_confidence_values(engine, TEXT) == {"eng": 0.9, "fra": 0.1}


def test_the_segmentation_adapter_requires_the_lingua_method() -> None:
    with pytest.raises(LanguageDetectionError) as caught:
        detect_multiple_languages_of(object(), TEXT)
    assert str(caught.value) == "Lingua detector lacks multiple-language detection"


def test_the_segmentation_adapter_returns_distinct_codes_in_order() -> None:
    engine = SimpleNamespace(
        detect_multiple_languages_of=lambda text: [
            SimpleNamespace(language=_language("ENG")),
            SimpleNamespace(language=_language("FRA")),
            SimpleNamespace(language=_language("ENG")),
        ]
    )

    assert detect_multiple_languages_of(engine, TEXT) == ("eng", "fra")


def test_the_segmentation_adapter_accepts_raw_language_results() -> None:
    language = _language("ENG")
    engine = SimpleNamespace(
        detect_multiple_languages_of=lambda text: [language, language],
    )

    assert detect_multiple_languages_of(engine, TEXT) == ("eng",)


def test_language_lookup_matches_lingua_codes_case_insensitively() -> None:
    language = _language("ENG")
    enum = SimpleNamespace(all=lambda: [language])

    assert detector_module._language_for_code(enum, "eng") is language


def test_an_absent_lingua_distribution_fails_closed(monkeypatch: pytest.MonkeyPatch) -> None:
    def _absent(name: str) -> str:
        raise metadata.PackageNotFoundError(name)

    monkeypatch.setattr(detector_module.metadata, "version", _absent)

    with pytest.raises(LanguageLibraryVersionError) as caught:
        build_lingua_detector()
    assert str(caught.value) == (
        "lingua-language-detector is not installed; install the language extra"
    )


def test_an_unpinned_lingua_version_fails_closed(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(detector_module.metadata, "version", lambda name: "1.0.0")

    with pytest.raises(LanguageLibraryVersionError) as caught:
        build_lingua_detector()
    assert str(caught.value) == (
        "installed lingua-language-detector version '1.0.0'; required '2.2.0'"
    )


def test_a_version_that_changes_during_construction_fails_closed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    identity = detector_module.language_model_identity(DEFAULT_LANGUAGE_POLICY)
    monkeypatch.setattr(detector_module.metadata, "version", lambda name: "2.2.0")
    monkeypatch.setattr(
        detector_module,
        "language_model_identity",
        lambda policy, **kwargs: SimpleNamespace(library_version="9.9.9", policy=identity.policy),
    )

    with pytest.raises(LanguageLibraryVersionError) as caught:
        build_lingua_detector()
    assert str(caught.value) == "Lingua identity version changed during construction"


def test_an_unimportable_lingua_package_is_reported(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(detector_module.metadata, "version", lambda name: "2.2.0")
    monkeypatch.setitem(__import__("sys").modules, "lingua", None)

    with pytest.raises(LanguageDetectionError) as caught:
        build_lingua_detector()
    assert str(caught.value) == "could not import the pinned Lingua package"


def test_an_unsupported_requested_code_is_reported() -> None:
    enum = SimpleNamespace(all=lambda: [_language("ENG")])

    with pytest.raises(LanguageDetectionError) as caught:
        detector_module._language_for_code(enum, "zzz")
    assert str(caught.value) == "Lingua does not support ISO 639-3 code 'zzz'"


def test_an_unusable_language_enum_is_reported() -> None:
    with pytest.raises(LanguageDetectionError) as caught:
        detector_module._all_languages(object())
    assert str(caught.value) == "Lingua language enum is unavailable"

    with pytest.raises(LanguageDetectionError) as caught:
        detector_module._all_languages(SimpleNamespace(all=lambda: 7))
    assert str(caught.value) == "Lingua language enum did not return languages"

    with pytest.raises(LanguageDetectionError) as caught:
        detector_module._all_languages(SimpleNamespace(all=lambda: "eng"))
    assert str(caught.value) == "Lingua language enum did not return languages"


def test_an_unusable_builder_is_reported() -> None:
    with pytest.raises(LanguageDetectionError) as caught:
        detector_module._builder_call(object(), "from_all_languages", ())
    assert str(caught.value) == "Lingua builder lacks from_all_languages"

    without_build = SimpleNamespace(from_all_languages=lambda: object())
    with pytest.raises(LanguageDetectionError) as caught:
        detector_module._builder_call(without_build, "from_all_languages", ())
    assert str(caught.value) == "Lingua builder lacks build"


def test_builder_call_invokes_the_factory_and_build_method() -> None:
    argument = object()
    built = object()
    calls: list[tuple[object, ...]] = []

    class FakeBuilder:
        @classmethod
        def from_languages(cls, *languages: object) -> "FakeBuilder":
            calls.append(languages)
            return cls()

        def build(self) -> object:
            return built

    assert detector_module._builder_call(FakeBuilder, "from_languages", (argument,)) is built
    assert calls == [(argument,)]


def test_a_bounded_language_scope_is_normalized() -> None:
    assert detector_module._requested_codes(["FRA", "eng"]) == ("eng", "fra")

    with pytest.raises(ValueError) as caught:
        detector_module._requested_codes("eng")
    assert str(caught.value) == "language_codes must be a sequence of ISO 639-3 codes"
    with pytest.raises(ValueError) as caught:
        detector_module._requested_codes([])
    assert str(caught.value) == "language_codes must not be empty"
    with pytest.raises(ValueError) as caught:
        detector_module._requested_codes(["eng", "ENG"])
    assert str(caught.value) == "language_codes must not contain duplicates"
