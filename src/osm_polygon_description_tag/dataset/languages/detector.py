"""Lazy, pinned Lingua adapter and conservative score classification."""

import importlib.metadata as metadata
import math
from collections.abc import Callable, Iterable, Mapping, Sequence
from typing import Protocol, cast

from osm_polygon_description_tag.dataset.languages.models import (
    DEFAULT_LANGUAGE_POLICY,
    LINGUA_LIBRARY_NAME,
    PINNED_LINGUA_VERSION,
    LanguageModelIdentity,
    LanguagePolicy,
    LanguageResult,
    LanguageStatus,
    language_model_identity,
)
from osm_polygon_description_tag.dataset.languages.records import DescriptionEntry


class LanguageDetectionError(RuntimeError):
    """Raised when a detector violates its confidence-value contract."""


class LanguageLibraryVersionError(LanguageDetectionError):
    """Raised when the installed Lingua distribution is not the pinned version."""


class LanguageDetectionCallable(Protocol):
    def __call__(self, text: str) -> LanguageResult: ...


ConfidenceComputer = Callable[[str], Mapping[str, float]]
MixedLanguageComputer = Callable[[str], Sequence[str]]


def _is_iso639_3(value: object) -> bool:
    if not isinstance(value, str) or len(value) != 3:
        return False
    return value.isascii() and value.isalpha()


def _language_code(value: object) -> str:
    if not isinstance(value, str):
        raise LanguageDetectionError("language code must be a string")
    if not _is_iso639_3(value):
        raise LanguageDetectionError("language code must be ISO 639-3")
    return value.lower()


def _coerce_score(value: object) -> float:
    if isinstance(value, bool) or not isinstance(value, int | float | str | bytes):
        raise LanguageDetectionError("confidence score must be numeric")
    try:
        return float(value)
    except (TypeError, ValueError) as error:
        raise LanguageDetectionError("confidence score must be numeric") from error


def _score_in_range(value: float) -> bool:
    return math.isfinite(value) and 0 <= value <= 1


def _score(value: object) -> float:
    numeric = _coerce_score(value)
    if not _score_in_range(numeric):
        raise LanguageDetectionError("confidence score must be finite and between 0 and 1")
    return numeric


def _ranked_scores(values: Mapping[str, float] | object) -> tuple[tuple[str, float], ...]:
    if not isinstance(values, Mapping):
        raise LanguageDetectionError("confidence provider must return a mapping")
    ranked: list[tuple[str, float]] = []
    seen: set[str] = set()
    for raw_code, raw_score in values.items():
        code = _language_code(raw_code)
        if code in seen:
            raise LanguageDetectionError(f"duplicate language code: {code}")
        seen.add(code)
        ranked.append((code, _score(raw_score)))
    return tuple(sorted(ranked, key=lambda item: -item[1]))


def _no_language_result(status: LanguageStatus, reason: str) -> LanguageResult:
    return LanguageResult(None, None, None, None, status, reason)


def _letter_count(text: str) -> int:
    return sum(character.isalpha() for character in text)


def _initial_result(text: str, policy: LanguagePolicy) -> LanguageResult | None:
    if not isinstance(text, str):
        raise TypeError("text must be a string")
    if not text.strip():
        return _no_language_result(LanguageStatus.NON_LINGUISTIC, "empty_or_whitespace")
    letters = _letter_count(text)
    if letters == 0:
        return _no_language_result(LanguageStatus.NON_LINGUISTIC, "no_letters")
    if letters < policy.min_alphabetic_chars:
        return _no_language_result(LanguageStatus.UNCERTAIN, "too_short")
    return None


def _model_input(text: str) -> str:
    return " ".join(text.split())


def _threshold_reason(
    top_score: float,
    margin: float | None,
    policy: LanguagePolicy,
) -> str | None:
    if top_score < policy.min_score:
        return "low_confidence"
    if margin is not None and margin < policy.min_margin:
        return "low_margin"
    return None


def _has_mixed_evidence(
    text: str,
    policy: LanguagePolicy,
    provider: MixedLanguageComputer | None,
) -> bool:
    if provider is None:
        return False
    if not _segmentation_is_eligible(text, policy):
        return False
    codes = {_language_code(code) for code in provider(text)}
    return len(codes) > 1


def _segmentation_is_eligible(text: str, policy: LanguagePolicy) -> bool:
    return _letter_count(text) >= 2 * policy.min_alphabetic_chars


def _score_details(
    ranked: tuple[tuple[str, float], ...],
) -> tuple[str, float, float | None, float | None] | None:
    if not ranked:
        return None
    top_code, top_score = ranked[0]
    runner_up_score = ranked[1][1] if len(ranked) > 1 else None
    margin = top_score - runner_up_score if runner_up_score is not None else None
    return top_code, top_score, runner_up_score, margin


def _score_reason(
    top_score: float,
    margin: float | None,
    text: str,
    policy: LanguagePolicy,
    mixed_provider: MixedLanguageComputer | None,
) -> str | None:
    if margin is not None and margin <= policy.tie_epsilon:
        return "tie"
    if _has_mixed_evidence(text, policy, mixed_provider):
        return "mixed_text"
    return _threshold_reason(top_score, margin, policy)


def _uncertain_score_result(
    top_score: float,
    runner_up_score: float | None,
    margin: float | None,
    reason: str,
) -> LanguageResult:
    return LanguageResult(
        None,
        top_score,
        runner_up_score,
        margin,
        LanguageStatus.UNCERTAIN,
        reason,
    )


def _score_result(
    ranked: tuple[tuple[str, float], ...],
    text: str,
    policy: LanguagePolicy,
    mixed_provider: MixedLanguageComputer | None,
) -> LanguageResult:
    details = _score_details(ranked)
    if details is None:
        return _no_language_result(LanguageStatus.UNCERTAIN, "no_confidence_values")
    top_code, top_score, runner_up_score, margin = details
    reason = _score_reason(top_score, margin, text, policy, mixed_provider)
    if reason is not None:
        return _uncertain_score_result(top_score, runner_up_score, margin, reason)
    return LanguageResult(
        top_code,
        top_score,
        runner_up_score,
        margin,
        LanguageStatus.DETECTED,
        "detected",
    )


class LanguageDetector:
    """Callable detector using an injected confidence provider.

    The default construction path is :func:`build_lingua_detector`; injecting
    the small confidence callable keeps processing tests deterministic without
    coupling them to Lingua internals.
    """

    __slots__ = ("_confidence_values", "_identity", "_mixed_provider", "_policy")

    def __init__(
        self,
        confidence_values: ConfidenceComputer,
        *,
        policy: LanguagePolicy = DEFAULT_LANGUAGE_POLICY,
        multiple_language_codes: MixedLanguageComputer | None = None,
        identity: LanguageModelIdentity | None = None,
    ) -> None:
        if not callable(confidence_values):
            raise TypeError("confidence_values must be callable")
        if not isinstance(policy, LanguagePolicy):
            raise TypeError("policy must be a LanguagePolicy")
        resolved_identity = identity or language_model_identity(policy)
        if resolved_identity.policy != policy:
            raise ValueError("model identity policy does not match detector policy")
        self._confidence_values = confidence_values
        self._mixed_provider = multiple_language_codes
        self._policy = policy
        self._identity = resolved_identity

    @property
    def identity(self) -> LanguageModelIdentity:
        return self._identity

    @property
    def policy(self) -> LanguagePolicy:
        return self._policy

    def __call__(self, text: str) -> LanguageResult:
        initial = _initial_result(text, self._policy)
        if initial is not None:
            return initial
        model_text = _model_input(text)
        ranked = _ranked_scores(self._confidence_values(model_text))
        return _score_result(ranked, model_text, self._policy, self._mixed_provider)


def compute_language_confidence_values(detector: object, text: str) -> dict[str, float]:
    """Adapt Lingua ``ConfidenceValue`` items to lowercase ISO 639-3 keys."""
    method = getattr(detector, "compute_language_confidence_values", None)
    if not callable(method):
        raise LanguageDetectionError("Lingua detector lacks confidence-value computation")
    return _converted_confidence_values(method(text))


def _confidence_item(item: object) -> tuple[object, object]:
    language = getattr(item, "language", None)
    raw_score = getattr(item, "value", None)
    if language is None or raw_score is None:
        raise LanguageDetectionError("Lingua confidence item must contain language and value")
    return language, raw_score


def _confidence_items(values: object) -> Iterable[object]:
    try:
        typed_values = cast(Iterable[object], values)  # pragma: no mutate - static narrowing
        return iter(typed_values)
    except TypeError as error:
        raise LanguageDetectionError("Lingua confidence values must be iterable") from error


def _converted_confidence_values(values: object) -> dict[str, float]:
    converted: dict[str, float] = {}
    for item in _confidence_items(values):
        language, raw_score = _confidence_item(item)
        code = _language_code(_lingua_iso_code(language))
        if code in converted:
            raise LanguageDetectionError(f"duplicate language code: {code}")
        converted[code] = _score(raw_score)
    return converted


def _lingua_iso_code(language: object) -> str:
    iso_code = getattr(language, "iso_code_639_3", None)
    name = getattr(iso_code, "name", None)
    if not isinstance(name, str):
        raise LanguageDetectionError("Lingua language lacks an ISO 639-3 code")
    return name


def detect_multiple_languages_of(detector: object, text: str) -> tuple[str, ...]:
    """Adapt Lingua segmentation results to distinct ISO 639-3 codes."""
    method = getattr(detector, "detect_multiple_languages_of", None)
    if not callable(method):
        raise LanguageDetectionError("Lingua detector lacks multiple-language detection")
    codes: list[str] = []
    for result in method(text):
        language = getattr(result, "language", result)
        code = _language_code(_lingua_iso_code(language))
        if code not in codes:
            codes.append(code)
    return tuple(codes)


class _LinguaAdapter:
    def __init__(self, detector: object) -> None:
        self._detector = detector

    def __call__(self, text: str) -> dict[str, float]:
        return compute_language_confidence_values(self._detector, text)

    def multiple_language_codes(self, text: str) -> tuple[str, ...]:
        return detect_multiple_languages_of(self._detector, text)


def _installed_lingua_version() -> str:
    try:
        installed = metadata.version(LINGUA_LIBRARY_NAME)
    except metadata.PackageNotFoundError as error:
        raise LanguageLibraryVersionError(
            f"{LINGUA_LIBRARY_NAME} is not installed; install the language extra"
        ) from error
    if installed != PINNED_LINGUA_VERSION:
        raise LanguageLibraryVersionError(
            f"installed {LINGUA_LIBRARY_NAME} version {installed!r}; "
            f"required {PINNED_LINGUA_VERSION!r}"
        )
    return installed


def _requested_codes(language_codes: Sequence[str]) -> tuple[str, ...]:
    if isinstance(language_codes, str | bytes):
        raise ValueError("language_codes must be a sequence of ISO 639-3 codes")
    normalized = tuple(sorted(_language_code(code) for code in language_codes))
    if not normalized:
        raise ValueError("language_codes must not be empty")
    if len(set(normalized)) != len(normalized):
        raise ValueError("language_codes must not contain duplicates")
    return normalized


def _language_for_code(language_enum: object, code: str) -> object:
    for language in _all_languages(language_enum):
        if _lingua_iso_code(language).lower() == code:
            return language
    raise LanguageDetectionError(f"Lingua does not support ISO 639-3 code {code!r}")


def _all_languages(language_enum: object) -> Iterable[object]:
    all_languages = getattr(language_enum, "all", None)
    if not callable(all_languages):
        raise LanguageDetectionError("Lingua language enum is unavailable")
    languages = all_languages()
    if isinstance(languages, str | bytes) or not isinstance(languages, Iterable):
        raise LanguageDetectionError("Lingua language enum did not return languages")
    return languages


def _builder_call(builder_type: object, method_name: str, arguments: tuple[object, ...]) -> object:
    method = getattr(builder_type, method_name, None)
    if not callable(method):
        raise LanguageDetectionError(f"Lingua builder lacks {method_name}")
    builder = method(*arguments)
    build = getattr(builder, "build", None)
    if not callable(build):
        raise LanguageDetectionError("Lingua builder lacks build")
    return build()


def _build_lingua_engine(
    builder_type: object,
    language_enum: object,
    language_codes: tuple[str, ...] | None,
) -> object:
    if language_codes is None:
        return _builder_call(builder_type, "from_all_languages", ())
    languages = tuple(_language_for_code(language_enum, code) for code in language_codes)
    return _builder_call(builder_type, "from_languages", languages)


def build_lingua_detector(
    policy: LanguagePolicy = DEFAULT_LANGUAGE_POLICY,
    *,
    language_codes: Sequence[str] | None = None,
) -> LanguageDetector:
    """Construct the pinned high-accuracy Lingua detector lazily.

    With no ``language_codes`` the builder includes all supported languages and
    leaves their models lazy. A bounded code list is provided for tiny smoke
    tests or deliberately scoped experiments; it does not change the default.
    This adapter makes no claim that unsupported or ambiguous text is detected
    perfectly.
    """
    installed_version = _installed_lingua_version()
    try:
        from lingua import Language, LanguageDetectorBuilder
    except ImportError as error:
        raise LanguageDetectionError("could not import the pinned Lingua package") from error
    requested = None if language_codes is None else _requested_codes(language_codes)
    engine = _build_lingua_engine(LanguageDetectorBuilder, Language, requested)
    adapter = _LinguaAdapter(engine)
    scope = ("all_supported",) if requested is None else requested
    identity = language_model_identity(policy, language_scope=scope)
    if installed_version != identity.library_version:
        raise LanguageLibraryVersionError("Lingua identity version changed during construction")
    return LanguageDetector(
        adapter,
        policy=policy,
        multiple_language_codes=adapter.multiple_language_codes,
        identity=identity,
    )


def detect_description_entries(
    entries: Iterable[DescriptionEntry],
    detector: LanguageDetectionCallable,
) -> tuple[tuple[DescriptionEntry, LanguageResult], ...]:
    """Return exactly one result paired with each actual description entry."""
    return tuple((entry, detector(entry.original_text)) for entry in entries)


__all__ = [
    "LanguageDetectionCallable",
    "LanguageDetectionError",
    "LanguageDetector",
    "LanguageLibraryVersionError",
    "build_lingua_detector",
    "compute_language_confidence_values",
    "detect_description_entries",
    "detect_multiple_languages_of",
]
