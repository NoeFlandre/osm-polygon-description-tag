"""Exact wiring contracts of the Lingua-primary, GlotLID-fallback cascade.

The cascade decides the published `language_code` of every annotation row, so
these pin what is actually handed to each detector: the caller's policy, the
caller's language scope, the caller's model path, and the exact text.
"""

from pathlib import Path

import pytest

from osm_polygon_description_tag.dataset.languages import detector as detector_module
from osm_polygon_description_tag.dataset.languages.detector import (
    FallbackLanguageDetector,
    LanguageDetectionError,
    build_language_detector,
)
from osm_polygon_description_tag.dataset.languages.models import (
    LanguagePolicy,
    LanguageResult,
    LanguageStatus,
)


def _result(status: LanguageStatus, code: str | None = "eng") -> LanguageResult:
    if status is LanguageStatus.DETECTED:
        return LanguageResult(code, 0.9, 0.1, 0.8, status, "detected")
    return LanguageResult(None, None, None, None, status, str(status))


class _Recorder:
    def __init__(self, result: LanguageResult) -> None:
        self._result = result
        self.texts: list[str] = []

    def __call__(self, text: str) -> LanguageResult:
        self.texts.append(text)
        return self._result


@pytest.mark.parametrize(
    ("bad", "message"),
    [
        ("primary", "primary detector must be callable"),
        ("fallback", "fallback detector must be callable"),
    ],
)
def test_a_non_callable_detector_is_refused_under_its_own_name(bad: str, message: str) -> None:
    detectors: dict[str, object] = {
        "primary": _Recorder(_result(LanguageStatus.DETECTED)),
        "fallback": _Recorder(_result(LanguageStatus.DETECTED)),
    }
    detectors[bad] = object()

    with pytest.raises(TypeError) as caught:
        FallbackLanguageDetector(detectors["primary"], detectors["fallback"])  # type: ignore[arg-type]

    assert str(caught.value) == message


def test_a_cascade_without_an_identity_refuses_to_invent_one() -> None:
    cascade = FallbackLanguageDetector(
        _Recorder(_result(LanguageStatus.DETECTED)),
        _Recorder(_result(LanguageStatus.DETECTED)),
    )

    with pytest.raises(LanguageDetectionError) as caught:
        cascade.identity  # noqa: B018

    assert str(caught.value) == "fallback detector has no model identity"


def test_the_primary_receives_the_exact_text_and_a_confident_result_is_kept() -> None:
    primary = _Recorder(_result(LanguageStatus.DETECTED, "fra"))
    fallback = _Recorder(_result(LanguageStatus.DETECTED, "eng"))
    cascade = FallbackLanguageDetector(primary, fallback)

    result = cascade("Un texte en français")

    assert primary.texts == ["Un texte en français"]
    assert fallback.texts == []
    assert result.language_code == "fra"


def test_the_fallback_receives_the_exact_text_only_when_the_primary_is_uncertain() -> None:
    primary = _Recorder(_result(LanguageStatus.UNCERTAIN))
    fallback = _Recorder(_result(LanguageStatus.DETECTED, "fra"))
    cascade = FallbackLanguageDetector(primary, fallback)

    result = cascade("ambiguous")

    assert primary.texts == ["ambiguous"]
    assert fallback.texts == ["ambiguous"]
    assert result.language_code == "fra"
    assert result.reason == "fallback_glotlid_v3"


def test_an_unresolved_fallback_leaves_the_primary_result_untouched() -> None:
    primary_result = _result(LanguageStatus.UNCERTAIN)
    cascade = FallbackLanguageDetector(
        _Recorder(primary_result), _Recorder(_result(LanguageStatus.UNCERTAIN))
    )

    assert cascade("ambiguous") == primary_result


def test_a_non_linguistic_primary_result_never_reaches_the_fallback() -> None:
    fallback = _Recorder(_result(LanguageStatus.DETECTED, "fra"))
    cascade = FallbackLanguageDetector(_Recorder(_result(LanguageStatus.NON_LINGUISTIC)), fallback)

    result = cascade("123")

    assert fallback.texts == []
    assert result.status is LanguageStatus.NON_LINGUISTIC


def test_the_builder_passes_the_policy_scope_and_model_path_to_both_detectors(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """A dropped argument here would silently build a default-policy cascade."""
    lingua_calls: list[tuple[object, object]] = []
    glotlid_calls: list[tuple[object, object]] = []
    policy = LanguagePolicy(min_alphabetic_chars=11, min_score=0.95, min_margin=0.5)
    model_path = tmp_path / "model_v3.bin"

    class _Built:
        def __init__(self, scope: tuple[str, ...]) -> None:
            self.identity = type("Identity", (), {"language_scope": scope})()

        def __call__(self, text: str) -> LanguageResult:
            return _result(LanguageStatus.DETECTED)

    def fake_lingua(given_policy: object, *, language_codes: object = None) -> _Built:
        lingua_calls.append((given_policy, language_codes))
        return _Built(("fra", "eng"))

    def fake_glotlid(given_policy: object, *, model_path: object = None) -> _Built:
        glotlid_calls.append((given_policy, model_path))
        return _Built(("all_supported",))

    monkeypatch.setattr(detector_module, "build_lingua_detector", fake_lingua)
    monkeypatch.setattr(
        "osm_polygon_description_tag.dataset.languages.glotlid.build_glotlid_detector",
        fake_glotlid,
    )

    cascade = build_language_detector(
        policy, language_codes=["fra", "eng"], glotlid_model_path=model_path
    )

    assert lingua_calls == [(policy, ["fra", "eng"])]
    assert glotlid_calls == [(policy, model_path)]
    assert cascade.identity.policy == policy
    assert cascade.identity.language_scope == ("eng", "fra")
