"""Strict adapter and loader for the pinned GlotLID v3 fallback model."""

import importlib
import importlib.metadata as metadata
import math
import re
from collections.abc import Iterable, Mapping
from pathlib import Path
from typing import Final, Protocol, cast

from osm_polygon_description_tag.dataset.languages.detector import (
    LanguageDetectionError,
    LanguageDetector,
)
from osm_polygon_description_tag.dataset.languages.models import (
    DEFAULT_LANGUAGE_POLICY,
    GLOTLID_MODEL_FILENAME,
    GLOTLID_MODEL_REPOSITORY,
    GLOTLID_MODEL_REVISION,
    GLOTLID_MODEL_SHA256,
    GLOTLID_RUNTIME_LIBRARY_NAME,
    GLOTLID_RUNTIME_LIBRARY_VERSION,
    LanguagePolicy,
    glotlid_model_identity,
)
from osm_polygon_description_tag.dataset.manifest import file_sha256

GLOTLID_TOP_K: Final = 8
_LABEL_PATTERN: Final = re.compile(r"__label__(?P<code>[a-z]{3})_(?P<script>[A-Za-z]{4})\Z")


class GlotLIDLabelError(LanguageDetectionError):
    """Raised when a GlotLID output label is not a versioned ISO/script label."""


class _Predictor(Protocol):
    def predict(self, text: str, k: int) -> tuple[object, object]: ...


def _label_code(label: object) -> str:
    if not isinstance(label, str):
        raise GlotLIDLabelError("GlotLID labels must use the __label__ prefix")
    match = _LABEL_PATTERN.fullmatch(label)
    if match is None:
        raise GlotLIDLabelError("GlotLID labels must contain an ISO 639-3 code and script")
    return match["code"]


def _predictions(result: tuple[object, object]) -> tuple[tuple[object, object], ...]:
    labels, scores = result
    if isinstance(labels, str) or isinstance(scores, str):
        raise GlotLIDLabelError("GlotLID predictions must be sequences")
    try:
        typed_labels = cast(Iterable[object], labels)  # pragma: no mutate - static cast
        typed_scores = cast(Iterable[object], scores)  # pragma: no mutate - static cast
        pairs = tuple(zip(typed_labels, typed_scores, strict=True))
    except (TypeError, ValueError) as error:
        raise GlotLIDLabelError("GlotLID predictions must be equal-length sequences") from error
    return pairs


#: fastText accumulates its softmax in float32, so a confident prediction comes
#: back marginally over 1.0 --- values up to 1.0000100135803223 were observed on
#: real descriptions. That is rounding, not a probability, so scores within this
#: much of 1.0 are clamped to 1.0 and anything beyond it is still refused.
SCORE_ROUNDING_TOLERANCE: Final = 1e-4


def _score(value: object) -> float:
    if isinstance(value, bool) or not isinstance(value, int | float):
        raise GlotLIDLabelError("GlotLID scores must be numeric")
    score = float(value)
    if not math.isfinite(score) or not 0 <= score <= 1 + SCORE_ROUNDING_TOLERANCE:
        raise GlotLIDLabelError("GlotLID scores must be between 0 and 1")
    score = min(score, 1.0)
    return score


def _merge_score(values: dict[str, float], code: str, score: float) -> None:
    previous = values.get(code)
    if previous is None:
        values[code] = score
    else:
        values[code] = max(previous, score)


class _GlotLIDAdapter:
    """Turn fastText labels and scores into the generic detector provider shape."""

    def __init__(self, model: _Predictor) -> None:
        self._model = model

    def __call__(self, text: str) -> Mapping[str, float]:
        return self._values(self._model.predict(text, GLOTLID_TOP_K))

    @staticmethod
    def _values(result: tuple[object, object]) -> dict[str, float]:
        values: dict[str, float] = {}
        for label, raw_score in _predictions(result):
            _merge_score(values, _label_code(label), _score(raw_score))
        return values


def _installed_fasttext_version() -> str:
    try:
        installed = metadata.version(GLOTLID_RUNTIME_LIBRARY_NAME)
    except metadata.PackageNotFoundError as error:
        raise LanguageDetectionError(
            f"{GLOTLID_RUNTIME_LIBRARY_NAME} is not installed; install the language extra"
        ) from error
    if installed != GLOTLID_RUNTIME_LIBRARY_VERSION:
        raise LanguageDetectionError(
            f"installed {GLOTLID_RUNTIME_LIBRARY_NAME} version {installed!r}; "
            f"required {GLOTLID_RUNTIME_LIBRARY_VERSION!r}"
        )
    return installed


def _download_model() -> Path:
    try:
        from huggingface_hub import hf_hub_download
    except ImportError as error:
        raise LanguageDetectionError("could not import huggingface_hub for GlotLID") from error
    try:
        downloaded = hf_hub_download(
            repo_id=GLOTLID_MODEL_REPOSITORY,
            filename=GLOTLID_MODEL_FILENAME,
            revision=GLOTLID_MODEL_REVISION,
        )
    except (OSError, RuntimeError, ValueError) as error:
        raise LanguageDetectionError("could not download the pinned GlotLID v3 model") from error
    return Path(downloaded)


def _file_sha256(path: Path) -> str:
    try:
        return file_sha256(path)
    except OSError as error:
        raise LanguageDetectionError(f"could not hash the GlotLID model: {path}") from error


def _verified_model_path(model_path: Path | None) -> Path:
    path = _download_model() if model_path is None else model_path
    if path.is_symlink() or not path.is_file():
        raise LanguageDetectionError(f"GlotLID model must be a regular file: {path}")
    if _file_sha256(path) != GLOTLID_MODEL_SHA256:
        raise LanguageDetectionError("GlotLID model hash does not match the pinned v3 artifact")
    return path


def _load_fasttext_model(path: Path) -> _Predictor:
    try:
        fasttext = importlib.import_module("fasttext")
    except ImportError as error:
        raise LanguageDetectionError("could not import the pinned fastText runtime") from error
    loader = getattr(fasttext, "load_model", None)
    if not callable(loader):
        raise LanguageDetectionError("fastText runtime lacks load_model")
    try:
        model = loader(str(path))
    except (OSError, RuntimeError, ValueError) as error:
        raise LanguageDetectionError("could not load the pinned GlotLID v3 model") from error
    return cast(_Predictor, model)  # pragma: no mutate - static cast


def build_glotlid_detector(
    policy: LanguagePolicy = DEFAULT_LANGUAGE_POLICY,
    *,
    model_path: Path | None = None,
) -> LanguageDetector:
    """Construct the pinned GlotLID v3 detector lazily."""
    installed_version = _installed_fasttext_version()
    verified_path = _verified_model_path(model_path)
    detector = LanguageDetector(
        _GlotLIDAdapter(_load_fasttext_model(verified_path)),
        policy=policy,
        identity=glotlid_model_identity(policy),
    )
    if installed_version != detector.identity.runtime_library_version:
        raise LanguageDetectionError("fastText identity version changed during construction")
    return detector


__all__ = ["GlotLIDLabelError", "build_glotlid_detector"]
