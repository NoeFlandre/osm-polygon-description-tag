"""Exact call, score, and construction contracts of the GlotLID v3 adapter.

The fallback only runs on values Lingua called uncertain, so a silently wrong
top-k, a dropped policy, or a loosened score bound would change the published
`language_code` of real rows without failing anything loudly.
"""

from pathlib import Path
from typing import Any

import pytest

from osm_polygon_description_tag.dataset.languages import glotlid as glotlid_module
from osm_polygon_description_tag.dataset.languages.glotlid import (
    GLOTLID_TOP_K,
    GlotLIDLabelError,
    build_glotlid_detector,
)
from osm_polygon_description_tag.dataset.languages.models import (
    GLOTLID_MODEL_SHA256,
    GLOTLID_RUNTIME_LIBRARY_NAME,
    GLOTLID_RUNTIME_LIBRARY_VERSION,
    LanguagePolicy,
)


class _Model:
    """A fastText stand-in that records exactly how it was called."""

    def __init__(self, result: tuple[object, object]) -> None:
        self._result = result
        self.calls: list[tuple[object, object]] = []

    def predict(self, text: object, k: object) -> tuple[object, object]:
        self.calls.append((text, k))
        return self._result


def _adapter(result: tuple[object, object]) -> Any:
    return glotlid_module._GlotLIDAdapter(_Model(result))


def test_the_adapter_asks_the_model_for_the_pinned_top_k_of_the_exact_text() -> None:
    model = _Model(((), ()))
    adapter = glotlid_module._GlotLIDAdapter(model)

    adapter("Un texte en français")

    assert model.calls == [("Un texte en français", GLOTLID_TOP_K)]
    assert GLOTLID_TOP_K == 8


def test_labels_and_scores_are_paired_into_iso_codes() -> None:
    adapter = _adapter((["__label__fra_Latn", "__label__eng_Latn"], [0.8, 0.1]))

    assert adapter("x") == {"fra": 0.8, "eng": 0.1}


def test_a_repeated_code_keeps_its_highest_score() -> None:
    adapter = _adapter((["__label__fra_Latn", "__label__fra_Arab"], [0.2, 0.7]))

    assert adapter("x") == {"fra": 0.7}


@pytest.mark.parametrize(
    "result",
    [("__label__fra_Latn", [0.8]), (["__label__fra_Latn"], "0.8")],
)
def test_a_bare_string_instead_of_a_sequence_is_refused_exactly(
    result: tuple[object, object],
) -> None:
    with pytest.raises(GlotLIDLabelError) as caught:
        _adapter(result)("x")

    assert str(caught.value) == "GlotLID predictions must be sequences"


def test_unequal_label_and_score_sequences_are_refused_exactly() -> None:
    with pytest.raises(GlotLIDLabelError) as caught:
        _adapter((["__label__fra_Latn", "__label__eng_Latn"], [0.8]))("x")

    assert str(caught.value) == "GlotLID predictions must be equal-length sequences"


@pytest.mark.parametrize("label", [7, None, b"__label__fra_Latn"])
def test_a_non_string_label_is_refused_exactly(label: object) -> None:
    with pytest.raises(GlotLIDLabelError) as caught:
        _adapter(([label], [0.8]))("x")

    assert str(caught.value) == "GlotLID labels must use the __label__ prefix"


@pytest.mark.parametrize(
    "label", ["fra_Latn", "__label__fr_Latn", "__label__fra_Lat", "__label__FRA_Latn"]
)
def test_a_label_that_is_not_an_iso_code_and_script_is_refused_exactly(label: str) -> None:
    with pytest.raises(GlotLIDLabelError) as caught:
        _adapter(([label], [0.8]))("x")

    assert str(caught.value) == "GlotLID labels must contain an ISO 639-3 code and script"


@pytest.mark.parametrize("score", [True, "0.8", None, b"1"])
def test_a_non_numeric_score_is_refused_exactly(score: object) -> None:
    with pytest.raises(GlotLIDLabelError) as caught:
        _adapter((["__label__fra_Latn"], [score]))("x")

    assert str(caught.value) == "GlotLID scores must be numeric"


@pytest.mark.parametrize("score", [0.0, 1.0, 0, 1])
def test_both_ends_of_the_unit_interval_are_accepted(score: float) -> None:
    """The bounds are inclusive; narrowing either one would drop real rows."""
    adapter = _adapter((["__label__fra_Latn"], [score]))

    assert adapter("x") == {"fra": float(score)}


def _install_fake_fasttext(monkeypatch: pytest.MonkeyPatch, loader_calls: list[object]) -> None:
    class _FakeFastText:
        @staticmethod
        def load_model(path: object) -> _Model:
            loader_calls.append(path)
            return _Model((["__label__fra_Latn"], [0.9]))

    monkeypatch.setattr(
        glotlid_module.importlib,
        "import_module",
        lambda name: _FakeFastText if name == "fasttext" else pytest.fail(name),
    )
    monkeypatch.setattr(
        glotlid_module, "_installed_fasttext_version", lambda: GLOTLID_RUNTIME_LIBRARY_VERSION
    )


def _pinned_model_file(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    path = tmp_path / "model_v3.bin"
    path.write_bytes(b"not the real model")
    monkeypatch.setattr(glotlid_module, "_file_sha256", lambda _path: GLOTLID_MODEL_SHA256)
    return path


def test_the_model_is_loaded_from_the_exact_verified_path(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    loader_calls: list[object] = []
    _install_fake_fasttext(monkeypatch, loader_calls)
    path = _pinned_model_file(tmp_path, monkeypatch)

    build_glotlid_detector(model_path=path)

    assert loader_calls == [str(path)]


def test_the_requested_policy_reaches_the_constructed_detector(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The fallback must apply the caller's thresholds, not the defaults."""
    loader_calls: list[object] = []
    _install_fake_fasttext(monkeypatch, loader_calls)
    path = _pinned_model_file(tmp_path, monkeypatch)
    policy = LanguagePolicy(min_alphabetic_chars=11)

    detector = build_glotlid_detector(policy, model_path=path)

    assert detector.policy == policy
    assert detector.identity.policy == policy


def test_a_model_whose_digest_is_not_the_pinned_artifact_is_refused_exactly(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    loader_calls: list[object] = []
    _install_fake_fasttext(monkeypatch, loader_calls)
    path = tmp_path / "model_v3.bin"
    path.write_bytes(b"wrong bytes")

    with pytest.raises(Exception) as caught:
        build_glotlid_detector(model_path=path)

    assert str(caught.value) == "GlotLID model hash does not match the pinned v3 artifact"
    assert loader_calls == []


def test_the_pinned_runtime_version_is_read_for_the_pinned_distribution(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The version pin is worthless if it interrogates some other distribution."""
    queried: list[object] = []

    def version(name: object) -> str:
        queried.append(name)
        return GLOTLID_RUNTIME_LIBRARY_VERSION

    monkeypatch.setattr(glotlid_module.metadata, "version", version)

    assert glotlid_module._installed_fasttext_version() == GLOTLID_RUNTIME_LIBRARY_VERSION
    assert queried == [GLOTLID_RUNTIME_LIBRARY_NAME]


@pytest.mark.parametrize("score", [1.0000066757202148, 1.0000100135803223, 1.0 + 1e-6])
def test_float32_rounding_just_above_one_is_clamped_not_refused(score: float) -> None:
    """fastText really returns these, and refusing them loses real rows.

    Its probabilities are accumulated in float32, so a confident prediction
    comes back marginally over 1.0 --- 1.0000100135803223 was observed on 12 of
    194 Afghan descriptions. A probability above one is still impossible, so
    the value is clamped rather than stored as it arrived.
    """
    assert _adapter((["__label__fra_Latn"], [score]))("x") == {"fra": 1.0}


@pytest.mark.parametrize("score", [1.001, 1.01, 2.0, 1.0 + 1e-3])
def test_a_score_beyond_float32_rounding_is_still_refused_exactly(score: float) -> None:
    """The tolerance covers rounding only; it must not hide a broken model."""
    with pytest.raises(GlotLIDLabelError) as caught:
        _adapter((["__label__fra_Latn"], [score]))("x")

    assert str(caught.value) == "GlotLID scores must be between 0 and 1"


@pytest.mark.parametrize("score", [-0.001, -1e-3, float("inf"), float("nan")])
def test_a_score_below_zero_or_not_finite_is_refused_exactly(score: float) -> None:
    """Nothing rounds a probability below zero; there is no tolerance that way."""
    with pytest.raises(GlotLIDLabelError) as caught:
        _adapter((["__label__fra_Latn"], [score]))("x")

    assert str(caught.value) == "GlotLID scores must be between 0 and 1"


def test_a_score_inside_the_interval_is_never_altered() -> None:
    """Clamping must touch only the impossible values, never a real score."""
    assert _adapter((["__label__fra_Latn"], [0.87654321]))("x") == {"fra": 0.87654321}


def test_the_rounding_tolerance_is_an_inclusive_upper_bound() -> None:
    """The bound is inclusive like the others; excluding it drops a real row."""
    at_the_bound = 1.0 + glotlid_module.SCORE_ROUNDING_TOLERANCE

    assert _adapter((["__label__fra_Latn"], [at_the_bound]))("x") == {"fra": 1.0}
