import dataclasses
import math
import subprocess
import sys
import types

import pytest

import osm_polygon_description_tag.dataset.languages.detector as detector_module
from osm_polygon_description_tag.dataset.languages import (
    LanguageDetectionError,
    LanguageDetector,
    LanguageLibraryVersionError,
    LanguageModelIdentity,
    LanguagePolicy,
    LanguageResult,
    LanguageStatus,
    compute_language_confidence_values,
    detect_description_entries,
    language_model_identity,
)
from tests.helpers.messages import exactly


def _detector(
    values: dict[str, float],
    *,
    policy: LanguagePolicy | None = None,
    segments: object = None,
) -> LanguageDetector:
    return LanguageDetector(
        lambda _text: values,
        policy=policy or LanguagePolicy(),
        multiple_language_codes=segments,
    )


def test_policy_defaults_are_conservative_and_immutable() -> None:
    policy = LanguagePolicy()

    assert policy.min_alphabetic_chars == 5
    with pytest.raises(dataclasses.FrozenInstanceError):
        policy.min_alphabetic_chars = 9  # type: ignore[misc]


@pytest.mark.parametrize(
    "kwargs",
    [
        {"min_alphabetic_chars": 0},
        {"min_alphabetic_chars": -1},
        {"min_alphabetic_chars": True},
        {"tie_epsilon": math.nan},
        {"tie_epsilon": math.inf},
        {"tie_epsilon": -0.1},
        {"tie_epsilon": 1.1},
        {"tie_epsilon": math.inf},
        {"tie_epsilon": -0.1},
    ],
)
def test_policy_rejects_invalid_thresholds(kwargs: dict[str, object]) -> None:
    with pytest.raises((TypeError, ValueError)):
        LanguagePolicy(**kwargs)  # type: ignore[arg-type]


def test_confidence_adapter_uses_lowercase_iso639_3_codes_and_raw_scores() -> None:
    class ConfidenceValue:
        def __init__(self, language: object, value: float) -> None:
            self.language = language
            self.value = value

    class IsoCode:
        name = "ENG"

    class Language:
        iso_code_639_3 = IsoCode()

    class LinguaLikeDetector:
        def compute_language_confidence_values(self, text: str) -> list[ConfidenceValue]:
            assert text == "model input"
            return [ConfidenceValue(Language(), 0.8125)]

    assert compute_language_confidence_values(LinguaLikeDetector(), "model input") == {
        "eng": 0.8125
    }


@pytest.mark.parametrize("raw_score", [True, object(), "not-a-score"])
def test_confidence_adapter_rejects_non_numeric_scores(raw_score: object) -> None:
    class ConfidenceValue:
        def __init__(self, value: object) -> None:
            self.language = Language()
            self.value = value

    class IsoCode:
        name = "ENG"

    class Language:
        iso_code_639_3 = IsoCode()

    class LinguaLikeDetector:
        def compute_language_confidence_values(self, text: str) -> list[ConfidenceValue]:
            assert text == "model input"
            return [ConfidenceValue(raw_score)]

    with pytest.raises(LanguageDetectionError) as caught:
        compute_language_confidence_values(LinguaLikeDetector(), "model input")
    assert str(caught.value) == "confidence score must be numeric"


def test_detector_constructor_validates_dependency_contracts() -> None:
    with pytest.raises(TypeError) as caught:
        LanguageDetector(None)  # type: ignore[arg-type]
    assert str(caught.value) == "confidence_values must be callable"
    with pytest.raises(TypeError) as caught:
        LanguageDetector(lambda _text: {}, policy=object())  # type: ignore[arg-type]
    assert str(caught.value) == "policy must be a LanguagePolicy"

    policy = LanguagePolicy()
    mismatched_identity = language_model_identity(LanguagePolicy(min_alphabetic_chars=8))
    with pytest.raises(ValueError) as caught:
        LanguageDetector(
            lambda _text: {"eng": 0.9},
            policy=policy,
            identity=mismatched_identity,
        )
    assert str(caught.value) == "model identity policy does not match detector policy"


def test_language_result_rejects_non_lowercase_iso639_3_codes() -> None:
    for code in ("EN", "ENG"):
        with pytest.raises(
            ValueError, match=exactly("language_code must be a lowercase ISO 639-3 code")
        ):
            LanguageResult(code, 0.9, None, None, LanguageStatus.DETECTED, "detected")


@pytest.mark.parametrize(
    "values",
    [
        (LanguageStatus.DETECTED, None, 0.9, None, None),
        (LanguageStatus.DETECTED, "eng", None, None, None),
        (LanguageStatus.UNCERTAIN, "eng", 0.9, None, None),
        (LanguageStatus.NON_LINGUISTIC, None, 0.9, None, None),
        (LanguageStatus.NON_LINGUISTIC, None, None, 0.1, None),
        (LanguageStatus.NON_LINGUISTIC, None, None, None, 0.1),
        (LanguageStatus.UNCERTAIN, None, 0.8, 0.9, 0.0),
        (LanguageStatus.UNCERTAIN, None, 0.8, 0.2, 0.5),
    ],
)
def test_language_result_rejects_status_and_score_inconsistencies(
    values: tuple[LanguageStatus, str | None, float | None, float | None, float | None],
) -> None:
    status, language_code, top_score, runner_up_score, margin = values

    with pytest.raises(ValueError):
        LanguageResult(
            language_code,
            top_score,
            runner_up_score,
            margin,
            status,
            "test",
        )


def test_language_status_does_not_expose_an_error_outcome() -> None:
    with pytest.raises(ValueError):
        LanguageStatus("error")


def test_detect_normalizes_only_model_input_and_returns_one_raw_score_result() -> None:
    seen: list[str] = []

    def confidence_values(text: str) -> dict[str, float]:
        seen.append(text)
        return {"eng": 0.95, "fra": 0.11}

    detector = LanguageDetector(confidence_values)
    result = detector("  Café\nnear\tthe river  ")

    assert seen == ["Café near the river"]
    assert result == detector("Café near the river")
    assert result.language_code == "eng"
    assert result.top_score == 0.95
    assert result.runner_up_score == 0.11
    assert result.margin == pytest.approx(0.84)
    assert result.status is LanguageStatus.DETECTED
    assert result.reason == "detected"


@pytest.mark.parametrize(
    "text",
    ["", " \n\t ", "1234---!!!"],
)
def test_empty_whitespace_and_no_letter_text_is_non_linguistic_without_calling_provider(
    text: str,
) -> None:
    called = False

    def fail(_text: str) -> dict[str, float]:
        nonlocal called
        called = True
        raise AssertionError("non-linguistic text must not reach the model")

    result = LanguageDetector(fail)(text)

    assert result.status is LanguageStatus.NON_LINGUISTIC
    assert result.language_code is None
    assert result.top_score is None
    assert result.reason in {"empty_or_whitespace", "no_letters"}
    assert called is False


def test_short_text_is_uncertain_without_forcing_a_language() -> None:
    called = False

    def fail(_text: str) -> dict[str, float]:
        nonlocal called
        called = True
        raise AssertionError("short text must be guarded before model use")

    result = LanguageDetector(fail)("Hi!")

    assert result.status is LanguageStatus.UNCERTAIN
    assert result.language_code is None
    assert result.reason == "too_short"
    assert called is False


def test_low_confidence_and_low_margin_are_detected() -> None:
    """Confidence is no longer gated; only a tie or mixed text refuses a label.

    The raw scores stay on the row so a consumer can apply its own threshold.
    """
    low_score = _detector({"eng": 0.79, "fra": 0.1})("A long enough sentence for testing.")
    low_margin = _detector({"eng": 0.9, "fra": 0.75})("A long enough sentence for testing.")

    assert low_score.status is LanguageStatus.DETECTED
    assert low_score.language_code == "eng"
    assert low_score.reason == "detected"
    assert low_score.top_score == 0.79
    assert low_margin.status is LanguageStatus.DETECTED
    assert low_margin.language_code == "eng"
    assert low_margin.reason == "detected"
    assert low_margin.margin == pytest.approx(0.15)


def test_a_vanishingly_small_margin_is_still_a_tie() -> None:
    """Removing the margin threshold must not accept an outright tie."""
    result = _detector({"eng": 0.5, "fra": 0.5})("A long enough sentence for testing.")

    assert result.status is LanguageStatus.UNCERTAIN
    assert result.language_code is None
    assert result.reason == "tie"


@pytest.mark.parametrize("score", [0.0, 1.0])
def test_provider_scores_accept_the_inclusive_zero_and_one_boundaries(score: float) -> None:
    """A lone candidate is accepted at either boundary now confidence is ungated."""
    result = _detector({"eng": score})("A long enough sentence for testing.")

    assert result.top_score == score
    assert result.status is LanguageStatus.DETECTED


def test_highest_confidence_wins_even_when_its_code_sorts_later() -> None:
    result = _detector({"eng": 0.7, "fra": 0.95})("A long enough sentence for testing.")

    assert result.status is LanguageStatus.DETECTED
    assert result.language_code == "fra"


def test_a_single_confidence_value_has_no_runner_up_or_margin() -> None:
    result = _detector({"eng": 0.95})("A long enough sentence for testing.")

    assert result.status is LanguageStatus.DETECTED
    assert result.language_code == "eng"
    assert result.runner_up_score is None
    assert result.margin is None


def test_score_thresholds_are_inclusive_at_their_configured_boundaries() -> None:
    at_minimum_score = _detector({"eng": 0.8, "fra": 0.0})("A long enough sentence for testing.")
    # Binary-exact fractions exercise equality, not subtraction round-off.
    at_minimum_margin = _detector(
        {"eng": 0.75, "fra": 0.5}, policy=LanguagePolicy(min_alphabetic_chars=3)
    )("A long enough sentence for testing.")

    assert at_minimum_score.status is LanguageStatus.DETECTED
    assert at_minimum_score.language_code == "eng"
    assert at_minimum_margin.status is LanguageStatus.DETECTED
    assert at_minimum_margin.language_code == "eng"


def test_exact_ties_are_deterministic_and_uncertain() -> None:
    result = _detector({"fra": 0.9, "eng": 0.9})("A long enough sentence for testing.")

    assert result.status is LanguageStatus.UNCERTAIN
    assert result.language_code is None
    assert result.top_score == 0.9
    assert result.runner_up_score == 0.9
    assert result.margin == 0.0
    assert result.reason == "tie"


def test_tie_epsilon_includes_an_exact_zero_boundary() -> None:
    policy = LanguagePolicy(tie_epsilon=0.0)
    result = _detector({"eng": 0.9, "fra": 0.9}, policy=policy)(
        "A long enough sentence for testing."
    )

    assert result.status is LanguageStatus.UNCERTAIN
    assert result.reason == "tie"


@pytest.mark.parametrize("values", [{"eng": 1.01}, {"eng": -0.01}, {"eng": math.nan}])
def test_invalid_provider_scores_are_errors_not_uncertainty(values: dict[str, float]) -> None:
    with pytest.raises(LanguageDetectionError) as caught:
        _detector(values)("A long enough sentence for testing.")
    assert str(caught.value) == "confidence score must be finite and between 0 and 1"


def test_provider_errors_propagate_as_real_errors() -> None:
    error = RuntimeError("synthetic detector failure")

    def fail(_text: str) -> dict[str, float]:
        raise error

    with pytest.raises(RuntimeError, match="synthetic detector failure") as caught:
        LanguageDetector(fail)("A long enough sentence for testing.")
    assert caught.value is error


def test_empty_confidence_values_are_uncertain() -> None:
    result = _detector({})("A long enough sentence for testing.")

    assert result.status is LanguageStatus.UNCERTAIN
    assert result.reason == "no_confidence_values"


def test_mixed_segmentation_is_used_only_when_it_has_multiple_languages() -> None:
    calls: list[str] = []

    def segments(text: str) -> tuple[str, ...]:
        calls.append(text)
        return ("eng", "fra")

    detector = _detector(
        {"eng": 0.95, "fra": 0.1},
        segments=segments,
    )
    result = detector("English words and Français mots in one description.")

    assert result.status is LanguageStatus.UNCERTAIN
    assert result.language_code is None
    assert result.reason == "mixed_text"
    assert calls == ["English words and Français mots in one description."]


def test_mixed_segmentation_is_guarded_for_tiny_inputs() -> None:
    called = False

    def segments(_text: str) -> tuple[str, ...]:
        nonlocal called
        called = True
        return ("eng", "fra")

    result = _detector(
        {"eng": 0.95, "fra": 0.1},
        segments=segments,
    )("Hi")

    assert result.status is LanguageStatus.UNCERTAIN
    assert result.reason == "too_short"
    assert called is False


def test_mixed_segmentation_runs_at_exactly_twice_the_minimum_length() -> None:
    calls: list[str] = []

    def segments(text: str) -> tuple[str, ...]:
        calls.append(text)
        return ("eng", "fra")

    result = _detector(
        {"eng": 0.95, "fra": 0.05},
        policy=LanguagePolicy(min_alphabetic_chars=5),
        segments=segments,
    )("abcdefghij")

    assert calls == ["abcdefghij"]
    assert result.status is LanguageStatus.UNCERTAIN
    assert result.reason == "mixed_text"


def test_description_detection_returns_one_result_for_each_entry() -> None:
    from osm_polygon_description_tag.dataset.languages import DescriptionEntry

    entries = (
        DescriptionEntry("a.osm.pbf", "way", 1, "description", "English text"),
        DescriptionEntry("a.osm.pbf", "way", 1, "description:fr", "Texte français"),
    )
    detector = _detector({"eng": 0.95, "fra": 0.1})

    detected = detect_description_entries(entries, detector)

    assert len(detected) == 2
    assert [entry.tag_key for entry, _result in detected] == ["description", "description:fr"]
    assert [result.language_code for _entry, result in detected] == ["eng", "eng"]


def test_model_identity_pins_library_version_policy_and_not_an_artifact_hash() -> None:
    first = language_model_identity(LanguagePolicy())
    second = language_model_identity(LanguagePolicy())
    changed = language_model_identity(LanguagePolicy(min_alphabetic_chars=8))

    assert first == second
    assert first.library_name == "lingua-language-detector"
    assert first.library_version == "2.2.0"
    assert first.config_fingerprint
    assert first.config_fingerprint != changed.config_fingerprint
    assert first.policy_fingerprint != changed.policy_fingerprint
    assert first.binary_artifact_hash is None


def test_model_identity_normalizes_scope_and_computes_derived_fields() -> None:
    first = language_model_identity(LanguagePolicy(), language_scope=("fra", "eng", "fra"))
    second = language_model_identity(LanguagePolicy(), language_scope=("eng", "fra"))

    assert first == second
    assert first.language_scope == ("eng", "fra")
    with pytest.raises(TypeError):
        LanguageModelIdentity(
            policy=LanguagePolicy(),
            language_scope=("eng",),
            library_name="wrong",  # type: ignore[call-arg]
            library_version="0.0.0",  # type: ignore[call-arg]
            policy_fingerprint="wrong",  # type: ignore[call-arg]
            config_fingerprint="wrong",  # type: ignore[call-arg]
        )


def test_importing_languages_does_not_import_lingua() -> None:
    code = (
        "import sys; "
        "import osm_polygon_description_tag.dataset.languages; "
        "assert 'lingua' not in sys.modules"
    )
    completed = subprocess.run(  # noqa: S603 - fixed interpreter and inline assertion
        [sys.executable, "-c", code],
        check=False,
        capture_output=True,
        text=True,
    )

    assert completed.returncode == 0, completed.stderr


def test_noarg_lingua_builder_uses_all_supported_lazy_path(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[str] = []

    class FakeBuilder:
        @classmethod
        def from_all_languages(cls) -> "FakeBuilder":
            calls.append("from_all_languages")
            return cls()

        def build(self) -> object:
            return object()

    fake_lingua = types.ModuleType("lingua")
    fake_lingua.Language = type("Language", (), {})  # type: ignore[attr-defined]
    fake_lingua.LanguageDetectorBuilder = FakeBuilder  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "lingua", fake_lingua)

    detector = detector_module.build_lingua_detector()

    assert calls == ["from_all_languages"]
    assert detector.identity.language_scope == ("all_supported",)


def test_lingua_builder_preserves_policy_and_mixed_language_wiring(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def language(code: str) -> object:
        return types.SimpleNamespace(
            iso_code_639_3=types.SimpleNamespace(name=code),
        )

    class FakeEngine:
        def compute_language_confidence_values(self, text: str) -> list[object]:
            return [
                types.SimpleNamespace(language=language("ENG"), value=0.95),
                types.SimpleNamespace(language=language("FRA"), value=0.05),
            ]

        def detect_multiple_languages_of(self, text: str) -> list[object]:
            return [
                types.SimpleNamespace(language=language("ENG")),
                types.SimpleNamespace(language=language("FRA")),
            ]

    class FakeBuilder:
        @classmethod
        def from_all_languages(cls) -> "FakeBuilder":
            return cls()

        def build(self) -> FakeEngine:
            return FakeEngine()

    fake_lingua = types.ModuleType("lingua")
    fake_lingua.Language = type("Language", (), {})  # type: ignore[attr-defined]
    fake_lingua.LanguageDetectorBuilder = FakeBuilder  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "lingua", fake_lingua)
    monkeypatch.setattr(detector_module.metadata, "version", lambda _name: "2.2.0")

    policy = LanguagePolicy(min_alphabetic_chars=7)
    detector = detector_module.build_lingua_detector(policy)

    assert detector.policy == policy
    assert detector("abcdef").reason == "too_short"
    assert detector("abcdefghijklmno").reason == "mixed_text"


def test_lingua_builder_fails_closed_when_installed_version_is_not_pinned(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(detector_module.metadata, "version", lambda _name: "2.1.0")

    with pytest.raises(LanguageLibraryVersionError, match="2.2.0"):
        detector_module.build_lingua_detector(language_codes=("eng", "fra"))


def test_lingua_builder_rejects_invalid_language_scope_inputs() -> None:
    with pytest.raises(ValueError, match="sequence"):
        detector_module.build_lingua_detector(language_codes="eng")  # type: ignore[arg-type]
    with pytest.raises(ValueError, match="empty"):
        detector_module.build_lingua_detector(language_codes=())
    with pytest.raises(ValueError, match=exactly("language_codes must not contain duplicates")):
        detector_module.build_lingua_detector(language_codes=("eng", "eng"))
