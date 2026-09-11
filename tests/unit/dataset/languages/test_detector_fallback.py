"""TDD contracts for the Lingua-primary, GlotLID-fallback detector."""

import hashlib
import importlib.metadata as metadata
import sys
from pathlib import Path
from types import ModuleType, SimpleNamespace

import pytest

import osm_polygon_description_tag.dataset.languages.detector as detector_module
import osm_polygon_description_tag.dataset.languages.glotlid as glotlid_module
from osm_polygon_description_tag.dataset.languages.detector import (
    FallbackLanguageDetector,
    LanguageDetectionError,
    LanguageDetector,
)
from osm_polygon_description_tag.dataset.languages.glotlid import (
    GlotLIDLabelError,
    _GlotLIDAdapter,
    build_glotlid_detector,
)
from osm_polygon_description_tag.dataset.languages.models import (
    GLOTLID_MODEL_SHA256,
    LanguagePolicy,
    LanguageResult,
    LanguageStatus,
    cascade_model_identity,
    language_model_identity,
)


def _result(
    status: LanguageStatus,
    *,
    language_code: str | None = None,
    top_score: float | None = None,
    runner_up_score: float | None = None,
    margin: float | None = None,
    reason: str = "test",
) -> LanguageResult:
    return LanguageResult(
        language_code,
        top_score,
        runner_up_score,
        margin,
        status,
        reason,
    )


def test_a_detected_lingua_result_does_not_call_the_fallback() -> None:
    calls: list[str] = []
    primary_result = _result(
        LanguageStatus.DETECTED,
        language_code="fra",
        top_score=0.95,
        reason="detected",
    )

    def fallback(text: str) -> LanguageResult:
        calls.append(text)
        raise AssertionError("GlotLID must not run after Lingua resolves the text")

    detector = FallbackLanguageDetector(lambda _text: primary_result, fallback)

    assert detector("Le vieux pont") is primary_result
    assert calls == []


def test_an_uncertain_lingua_result_uses_a_detected_glotlid_result() -> None:
    calls: list[str] = []
    primary_result = _result(
        LanguageStatus.UNCERTAIN,
        top_score=0.61,
        runner_up_score=0.38,
        margin=0.23,
        reason="low_confidence",
    )
    fallback_result = _result(
        LanguageStatus.DETECTED,
        language_code="eng",
        top_score=0.91,
        runner_up_score=0.04,
        margin=0.87,
        reason="detected",
    )

    def fallback(text: str) -> LanguageResult:
        calls.append(text)
        return fallback_result

    detector = FallbackLanguageDetector(lambda _text: primary_result, fallback)

    result = detector("A long enough description")

    assert calls == ["A long enough description"]
    assert result == LanguageResult(
        "eng", 0.91, 0.04, 0.87, LanguageStatus.DETECTED, "fallback_glotlid_v3"
    )


def test_an_unresolved_fallback_keeps_the_original_lingua_result() -> None:
    primary_result = _result(
        LanguageStatus.UNCERTAIN,
        top_score=0.61,
        runner_up_score=0.60,
        margin=0.01,
        reason="low_confidence",
    )
    fallback_result = _result(
        LanguageStatus.UNCERTAIN,
        top_score=0.51,
        runner_up_score=0.49,
        margin=0.02,
        reason="low_margin",
    )

    detector = FallbackLanguageDetector(lambda _text: primary_result, lambda _text: fallback_result)

    assert detector("A long enough description") is primary_result


def test_non_linguistic_lingua_result_does_not_call_the_fallback() -> None:
    primary_result = _result(LanguageStatus.NON_LINGUISTIC, reason="no_letters")

    def fail(_text: str) -> LanguageResult:
        raise AssertionError("non-linguistic text must not reach GlotLID")

    assert FallbackLanguageDetector(lambda _text: primary_result, fail)("1234") is primary_result


def test_glotlid_adapter_converts_script_labels_to_iso_codes() -> None:
    model = SimpleNamespace(
        predict=lambda text, k: (
            ("__label__eng_Latn", "__label__fra_Latn"),
            (0.91, 0.07),
        )
    )

    assert _GlotLIDAdapter(model)("A description") == {"eng": 0.91, "fra": 0.07}


def test_glotlid_adapter_keeps_the_highest_score_for_duplicate_scripts() -> None:
    model = SimpleNamespace(
        predict=lambda text, k: (
            ("__label__eng_Latn", "__label__eng_Cyrl", "__label__fra_Latn"),
            (0.91, 0.02, 0.07),
        )
    )

    assert _GlotLIDAdapter(model)("A description") == {"eng": 0.91, "fra": 0.07}


@pytest.mark.parametrize(
    "labels",
    [
        ("eng",),
        ("__label__english_Latn",),
        ("__label__eng",),
    ],
)
def test_glotlid_adapter_rejects_non_v3_labels(labels: tuple[str, ...]) -> None:
    model = SimpleNamespace(predict=lambda text, k: (labels, (0.9,) * len(labels)))

    with pytest.raises(GlotLIDLabelError):
        _GlotLIDAdapter(model)("A description")


def test_glotlid_builder_verifies_and_loads_the_pinned_model(
    tmp_path, monkeypatch: pytest.MonkeyPatch
) -> None:
    model_path = tmp_path / "model_v3.bin"
    model_path.write_bytes(b"test model")
    fake_model = SimpleNamespace(predict=lambda text, k: (("__label__eng_Latn",), (0.95,)))
    fake_fasttext = ModuleType("fasttext")
    fake_fasttext.load_model = lambda path: fake_model  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "fasttext", fake_fasttext)
    monkeypatch.setattr(
        "osm_polygon_description_tag.dataset.languages.glotlid._installed_fasttext_version",
        lambda: "0.10.2",
    )
    monkeypatch.setattr(
        "osm_polygon_description_tag.dataset.languages.glotlid._file_sha256",
        lambda path: GLOTLID_MODEL_SHA256,
    )

    detector = build_glotlid_detector(model_path=model_path)

    assert detector.identity.binary_artifact_hash == GLOTLID_MODEL_SHA256
    assert detector("A description").language_code == "eng"


def test_language_builder_binds_the_cascade_identity_and_scope(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    policy = LanguagePolicy()
    primary = LanguageDetector(
        lambda _text: _result(LanguageStatus.UNCERTAIN, reason="low_confidence"),
        policy=policy,
        identity=language_model_identity(policy, language_scope=("eng", "fra")),
    )
    fallback = LanguageDetector(
        lambda _text: _result(
            LanguageStatus.DETECTED,
            language_code="eng",
            top_score=0.95,
            reason="detected",
        ),
        policy=policy,
    )
    monkeypatch.setattr(detector_module, "build_lingua_detector", lambda *args, **kwargs: primary)
    monkeypatch.setattr(
        "osm_polygon_description_tag.dataset.languages.glotlid.build_glotlid_detector",
        lambda *args, **kwargs: fallback,
    )

    detector = detector_module.build_language_detector(policy, language_codes=("fra", "eng"))

    assert detector.identity == cascade_model_identity(policy, language_scope=("eng", "fra"))


@pytest.mark.parametrize(
    "score",
    [True, "0.9"],
)
def test_glotlid_adapter_rejects_non_numeric_scores(score: object) -> None:
    model = SimpleNamespace(predict=lambda text, k: (("__label__eng_Latn",), (score,)))

    with pytest.raises(GlotLIDLabelError):
        _GlotLIDAdapter(model)("A description")


def test_glotlid_adapter_rejects_mismatched_prediction_lengths() -> None:
    model = SimpleNamespace(
        predict=lambda text, k: (("__label__eng_Latn", "__label__fra_Latn"), (0.9,))
    )

    with pytest.raises(GlotLIDLabelError):
        _GlotLIDAdapter(model)("A description")


def test_fasttext_runtime_version_is_required(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(glotlid_module.metadata, "version", lambda _name: "0.10.2")
    assert glotlid_module._installed_fasttext_version() == "0.10.2"

    monkeypatch.setattr(glotlid_module.metadata, "version", lambda _name: "0.10.1")
    with pytest.raises(LanguageDetectionError, match="required .0.10.2"):
        glotlid_module._installed_fasttext_version()


def test_missing_fasttext_runtime_fails_closed(monkeypatch: pytest.MonkeyPatch) -> None:
    def missing(name: str) -> str:
        raise metadata.PackageNotFoundError(name)

    monkeypatch.setattr(glotlid_module.metadata, "version", missing)
    with pytest.raises(LanguageDetectionError, match="is not installed"):
        glotlid_module._installed_fasttext_version()


def test_download_uses_the_pinned_hub_revision(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    received: dict[str, object] = {}
    model_path = tmp_path / "model_v3.bin"
    module = ModuleType("huggingface_hub")

    def download(**kwargs: object) -> str:
        received.update(kwargs)
        return str(model_path)

    module.hf_hub_download = download  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "huggingface_hub", module)

    assert glotlid_module._download_model() == model_path
    assert received == {
        "repo_id": glotlid_module.GLOTLID_MODEL_REPOSITORY,
        "filename": glotlid_module.GLOTLID_MODEL_FILENAME,
        "revision": glotlid_module.GLOTLID_MODEL_REVISION,
    }


def test_download_failure_is_wrapped(monkeypatch: pytest.MonkeyPatch) -> None:
    module = ModuleType("huggingface_hub")

    def fail(**_kwargs: object) -> str:
        raise RuntimeError("network down")

    module.hf_hub_download = fail  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "huggingface_hub", module)

    with pytest.raises(LanguageDetectionError, match="could not download"):
        glotlid_module._download_model()


def test_missing_huggingface_hub_is_wrapped(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setitem(sys.modules, "huggingface_hub", None)
    with pytest.raises(LanguageDetectionError, match="could not import huggingface_hub"):
        glotlid_module._download_model()


def test_file_hashes_are_streamed_and_missing_files_fail(tmp_path: Path) -> None:
    path = tmp_path / "model.bin"
    path.write_bytes(b"model")
    assert glotlid_module._file_sha256(path) == hashlib.sha256(b"model").hexdigest()

    with pytest.raises(LanguageDetectionError, match="could not hash"):
        glotlid_module._file_sha256(tmp_path / "missing.bin")


def test_verified_model_path_checks_file_kind_and_digest(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    model = tmp_path / "model.bin"
    model.write_bytes(b"model")
    monkeypatch.setattr(glotlid_module, "_file_sha256", lambda _path: GLOTLID_MODEL_SHA256)
    assert glotlid_module._verified_model_path(model) == model

    wrong = tmp_path / "wrong.bin"
    wrong.write_bytes(b"wrong")
    monkeypatch.setattr(glotlid_module, "_file_sha256", lambda _path: "0" * 64)
    with pytest.raises(LanguageDetectionError, match="does not match"):
        glotlid_module._verified_model_path(wrong)

    directory = tmp_path / "directory"
    directory.mkdir()
    with pytest.raises(LanguageDetectionError, match="regular file"):
        glotlid_module._verified_model_path(directory)

    linked = tmp_path / "linked.bin"
    linked.symlink_to(model)
    with pytest.raises(LanguageDetectionError, match="regular file"):
        glotlid_module._verified_model_path(linked)


def test_verified_model_path_downloads_when_no_path_is_supplied(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    model = tmp_path / "downloaded.bin"
    model.write_bytes(b"model")
    monkeypatch.setattr(glotlid_module, "_download_model", lambda: model)
    monkeypatch.setattr(glotlid_module, "_file_sha256", lambda _path: GLOTLID_MODEL_SHA256)
    assert glotlid_module._verified_model_path(None) == model


def test_fasttext_loader_reports_import_shape_and_load_errors(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setattr(
        glotlid_module.importlib,
        "import_module",
        lambda _name: (_ for _ in ()).throw(ImportError("missing")),
    )
    with pytest.raises(LanguageDetectionError, match="could not import"):
        glotlid_module._load_fasttext_model(tmp_path / "model.bin")

    module = ModuleType("fasttext")
    monkeypatch.setattr(glotlid_module.importlib, "import_module", lambda _name: module)
    with pytest.raises(LanguageDetectionError, match="lacks load_model"):
        glotlid_module._load_fasttext_model(tmp_path / "model.bin")

    def fail(_path: str) -> object:
        raise OSError("bad model")

    module.load_model = fail  # type: ignore[attr-defined]
    with pytest.raises(LanguageDetectionError, match="could not load"):
        glotlid_module._load_fasttext_model(tmp_path / "model.bin")

    expected = object()
    module.load_model = lambda _path: expected  # type: ignore[attr-defined]
    assert glotlid_module._load_fasttext_model(tmp_path / "model.bin") is expected


def test_builder_rejects_an_identity_that_changes_runtime_version(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    model = tmp_path / "model.bin"
    model.write_bytes(b"model")
    monkeypatch.setattr(glotlid_module, "_installed_fasttext_version", lambda: "0.10.2")
    monkeypatch.setattr(glotlid_module, "_verified_model_path", lambda _path: model)
    monkeypatch.setattr(glotlid_module, "_load_fasttext_model", lambda _path: SimpleNamespace())
    monkeypatch.setattr(
        glotlid_module,
        "glotlid_model_identity",
        lambda policy: SimpleNamespace(
            policy=policy, runtime_library_version="changed", config_fingerprint="fingerprint"
        ),
    )

    with pytest.raises(LanguageDetectionError, match="changed during construction"):
        build_glotlid_detector(model_path=model)


def _adapter_for(result: tuple[object, object]) -> _GlotLIDAdapter:
    """Drive the adapter with a stand-in model that returns ``result``."""
    return _GlotLIDAdapter(SimpleNamespace(predict=lambda text, k: result))


@pytest.mark.parametrize("score", [0.0, 0.5, 1.0])
def test_glotlid_accepts_scores_on_and_inside_the_probability_bounds(score: float) -> None:
    """Both bounds are inclusive, so neither endpoint may be refused."""
    assert _adapter_for((["__label__eng_Latn"], [score]))("hello there") == {"eng": score}


@pytest.mark.parametrize("score", [-0.01, 1.01, float("nan"), float("inf"), float("-inf")])
def test_glotlid_refuses_scores_outside_the_probability_bounds(score: float) -> None:
    with pytest.raises(GlotLIDLabelError) as error:
        _adapter_for((["__label__eng_Latn"], [score]))("hello there")

    assert str(error.value) == "GlotLID scores must be between 0 and 1"


@pytest.mark.parametrize("score", [True, False, "0.5", None, b"0.5", [0.5]])
def test_glotlid_refuses_a_non_numeric_score_by_name(score: object) -> None:
    """A bool is an int at runtime, so it has to be refused explicitly."""
    with pytest.raises(GlotLIDLabelError) as error:
        _adapter_for((["__label__eng_Latn"], [score]))("hello there")

    assert str(error.value) == "GlotLID scores must be numeric"


@pytest.mark.parametrize(
    "result",
    [
        ("__label__eng_Latn", [0.5]),
        (["__label__eng_Latn"], "0.5"),
        ("__label__eng_Latn", "0.5"),
    ],
)
def test_glotlid_refuses_string_prediction_sequences(result: tuple[object, object]) -> None:
    """A bare string would otherwise zip character by character."""
    with pytest.raises(GlotLIDLabelError) as error:
        _adapter_for(result)("hello there")

    assert str(error.value) == "GlotLID predictions must be sequences"


def test_glotlid_refuses_unequal_length_predictions() -> None:
    """Labels and scores are paired strictly; a short tail must not be dropped."""
    with pytest.raises(GlotLIDLabelError) as error:
        _adapter_for((["__label__eng_Latn", "__label__fra_Latn"], [0.5]))("hello there")

    assert str(error.value) == "GlotLID predictions must be equal-length sequences"
