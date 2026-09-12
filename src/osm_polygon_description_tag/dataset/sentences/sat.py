"""The pinned SaT-3l-sm sentence splitter, constructed lazily and verified.

SaT segments text without being told a language: the model is language-agnostic
at inference, and ``lang_code`` in the library's API only selects a style
adapter, which this configuration does not use. The language therefore does not
steer the model here --- it is the authorisation the gate already granted, and
this adapter refuses to be used against a language SaT was never trained on, so
the competence rule holds even if a caller bypasses the gate.

``model_dir`` is a directory, not a file: ``wtpsplit`` loads a model the way
``transformers`` does, from a directory holding ``config.json`` beside the
weights. The tokenizer is loaded from that same directory and never defaulted,
because the library's default fetches ``xlm-roberta-base`` from the Hub and a
compute node has no reason to have network access.

Nothing is imported or downloaded until a splitter is actually built, so the
whole module is importable on a machine that has neither wtpsplit nor torch.
"""

from __future__ import annotations

import hashlib
import importlib
from collections.abc import Iterable
from importlib import metadata
from pathlib import Path
from typing import Final, Protocol, cast

from osm_polygon_description_tag.dataset.sentences.languages import SAT_SUPPORTED_LANGUAGES

SPLITTER_NAME: Final = "sat-3l-sm"
SAT_MODEL_REPOSITORY: Final = "segment-any-text/sat-3l-sm"
SAT_MODEL_REVISION: Final = "137da054051ad9f1eac42025f758db4ac9f22535"
SAT_MODEL_FILENAME: Final = "model.safetensors"
SAT_CONFIG_FILENAME: Final = "config.json"
SAT_MODEL_SHA256: Final = "3e19cb0e5dbe9790d37d918d7e87880cb6577d497833f0f0627d80ae6ca1fe90"
SAT_RUNTIME_LIBRARY_NAME: Final = "wtpsplit"
SAT_RUNTIME_LIBRARY_VERSION: Final = "2.2.1"


class SentenceSplitterError(RuntimeError):
    """Raised when the pinned splitter cannot be built or trusted."""


class _Segmenter(Protocol):
    def split(self, text: str) -> object: ...


def _installed_wtpsplit_version() -> str:
    try:
        installed = metadata.version(SAT_RUNTIME_LIBRARY_NAME)
    except metadata.PackageNotFoundError as error:
        raise SentenceSplitterError(
            f"{SAT_RUNTIME_LIBRARY_NAME} is not installed; install the language extra"
        ) from error
    if installed != SAT_RUNTIME_LIBRARY_VERSION:
        raise SentenceSplitterError(
            f"installed {SAT_RUNTIME_LIBRARY_NAME} version {installed!r}; "
            f"required {SAT_RUNTIME_LIBRARY_VERSION!r}"
        )
    return installed


def _file_sha256(path: Path) -> str:
    # ``hashlib`` resolves digest names case-insensitively, so an upper-case spelling
    # selects the same algorithm and cannot change the digest this returns.
    digest_name = "sha256"  # pragma: no mutate - digest names are case-insensitive
    try:
        with path.open("rb") as handle:
            return hashlib.file_digest(handle, digest_name).hexdigest()
    except OSError as error:
        raise SentenceSplitterError(f"could not hash the SaT model: {path}") from error


def _verified_model_dir(model_dir: Path) -> Path:
    """Verify the staged directory holds exactly the pinned weights and its config."""
    _require_regular_directory(model_dir)
    _require_staged_files(model_dir)
    _require_pinned_weights(model_dir)
    return model_dir


def _require_regular_directory(model_dir: Path) -> None:
    if model_dir.is_symlink() or not model_dir.is_dir():
        raise SentenceSplitterError(f"SaT model directory must be a directory: {model_dir}")


def _require_staged_files(model_dir: Path) -> None:
    """``wtpsplit`` loads a directory, so the config has to travel with the weights."""
    for name in (SAT_MODEL_FILENAME, SAT_CONFIG_FILENAME):
        staged = model_dir / name
        if staged.is_symlink() or not staged.is_file():
            raise SentenceSplitterError(f"SaT model directory is missing {name}: {model_dir}")


def _require_pinned_weights(model_dir: Path) -> None:
    if _file_sha256(model_dir / SAT_MODEL_FILENAME) != SAT_MODEL_SHA256:
        raise SentenceSplitterError(
            f"SaT model hash does not match the pinned {SPLITTER_NAME} artifact"
        )


def _load_sat_model(model_dir: Path) -> _Segmenter:
    try:
        wtpsplit = importlib.import_module("wtpsplit")
    except ImportError as error:
        raise SentenceSplitterError(
            f"could not import the pinned {SAT_RUNTIME_LIBRARY_NAME} runtime"
        ) from error
    loader = getattr(wtpsplit, "SaT", None)
    if not callable(loader):
        raise SentenceSplitterError(f"{SAT_RUNTIME_LIBRARY_NAME} runtime lacks SaT")
    try:
        # The tokenizer is named explicitly: the library's default would fetch
        # ``xlm-roberta-base`` from the Hub, and this runs offline.
        model = loader(str(model_dir), tokenizer_name_or_path=str(model_dir))
    except (OSError, RuntimeError, ValueError) as error:
        raise SentenceSplitterError(f"could not load the pinned {SPLITTER_NAME} model") from error
    return cast(_Segmenter, model)  # pragma: no mutate - static cast


def _segments(raw: object) -> tuple[str, ...]:
    if isinstance(raw, str) or not isinstance(raw, Iterable):
        raise SentenceSplitterError("SaT segments must be a sequence")
    typed = cast(Iterable[object], raw)  # pragma: no mutate - static cast
    segments = tuple(typed)
    if any(not isinstance(segment, str) for segment in segments):
        raise SentenceSplitterError("SaT segments must be strings")
    return cast(tuple[str, ...], segments)  # pragma: no mutate - static cast


class _SaTAdapter:
    """Adapt the pinned SaT model to this project's narrow splitter surface."""

    def __init__(self, model: _Segmenter) -> None:
        self._model = model

    def split(self, text: str, *, language: str) -> tuple[str, ...]:
        """Return ``text`` segmented, refusing a language SaT was not trained on."""
        if language not in SAT_SUPPORTED_LANGUAGES:
            raise SentenceSplitterError(f"SaT was not trained on language {language!r}")
        return _segments(self._model.split(text))


def build_sat_splitter(*, model_dir: Path) -> _SaTAdapter:
    """Construct the pinned SaT-3l-sm splitter from a verified local directory."""
    _installed_wtpsplit_version()
    verified_dir = _verified_model_dir(model_dir)
    return _SaTAdapter(_load_sat_model(verified_dir))


__all__ = [
    "SAT_CONFIG_FILENAME",
    "SAT_MODEL_FILENAME",
    "SAT_MODEL_REPOSITORY",
    "SAT_MODEL_REVISION",
    "SAT_MODEL_SHA256",
    "SAT_RUNTIME_LIBRARY_NAME",
    "SAT_RUNTIME_LIBRARY_VERSION",
    "SPLITTER_NAME",
    "SentenceSplitterError",
    "build_sat_splitter",
]
