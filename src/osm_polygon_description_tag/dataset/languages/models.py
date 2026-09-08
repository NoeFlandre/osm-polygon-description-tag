"""Immutable contracts for conservative language detection."""

import hashlib
import json
import math
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Final

LINGUA_LIBRARY_NAME: Final = "lingua-language-detector"
PINNED_LINGUA_VERSION: Final = "2.2.0"
DEFAULT_LANGUAGE_SCOPE: Final = ("all_supported",)


class LanguageStatus(StrEnum):
    """Outcome category for one description's language detection."""

    DETECTED = "detected"
    UNCERTAIN = "uncertain"
    NON_LINGUISTIC = "non_linguistic"


def _validated_float(name: str, value: object, *, maximum: float) -> float:
    if isinstance(value, bool) or not isinstance(value, int | float):
        raise TypeError(f"{name} must be a real number")
    numeric = float(value)
    if not math.isfinite(numeric) or not 0 <= numeric <= maximum:
        raise ValueError(f"{name} must be finite and between 0 and {maximum}")
    return numeric


def _validate_positive_count(value: object) -> None:
    if type(value) is not int:
        raise TypeError("min_alphabetic_chars must be an integer")
    if value < 1:
        raise ValueError("min_alphabetic_chars must be positive")


@dataclass(frozen=True, slots=True)
class LanguagePolicy:
    """Conservative, explicit thresholds applied to raw Lingua scores."""

    min_alphabetic_chars: int = 5
    min_score: float = 0.8
    min_margin: float = 0.2
    tie_epsilon: float = 1e-12

    def __post_init__(self) -> None:
        _validate_positive_count(self.min_alphabetic_chars)
        object.__setattr__(
            self,
            "min_score",
            _validated_float("min_score", self.min_score, maximum=1.0),
        )
        object.__setattr__(
            self,
            "min_margin",
            _validated_float("min_margin", self.min_margin, maximum=1.0),
        )
        object.__setattr__(
            self,
            "tie_epsilon",
            _validated_float("tie_epsilon", self.tie_epsilon, maximum=1.0),
        )


def _is_lower_iso_code(value: object) -> bool:
    if not isinstance(value, str) or len(value) != 3:
        return False
    return value.isascii() and value.isalpha() and value == value.lower()


def _validate_language_code(value: object) -> None:
    if not _is_lower_iso_code(value):
        raise ValueError("language_code must be a lowercase ISO 639-3 code")


@dataclass(frozen=True, slots=True)
class LanguageResult:
    """One detection result with raw confidence scores, never probabilities."""

    language_code: str | None
    top_score: float | None
    runner_up_score: float | None
    margin: float | None
    status: LanguageStatus
    reason: str

    def __post_init__(self) -> None:
        _validate_optional_language_code(self.language_code)
        object.__setattr__(self, "status", _language_status(self.status))
        _validate_reason(self.reason)
        _normalize_result_scores(self)
        _validate_result_consistency(self)


def _validate_optional_language_code(value: str | None) -> None:
    if value is not None:
        _validate_language_code(value)


def _language_status(value: LanguageStatus | str) -> LanguageStatus:
    if isinstance(value, LanguageStatus):
        return value
    return LanguageStatus(value)


def _validate_reason(value: object) -> None:
    if not isinstance(value, str) or not value:
        raise ValueError("reason must be a non-empty string")


def _normalize_result_scores(result: LanguageResult) -> None:
    for name in ("top_score", "runner_up_score", "margin"):
        value = getattr(result, name)
        if value is not None:
            object.__setattr__(result, name, _validated_float(name, value, maximum=1.0))


def _validate_result_consistency(result: LanguageResult) -> None:
    _validate_status_fields(result)
    _validate_non_linguistic_scores(result)
    _validate_runner_up_score(result)
    _validate_margin(result)


def _validate_status_fields(result: LanguageResult) -> None:
    if result.status is LanguageStatus.DETECTED:
        if result.language_code is None or result.top_score is None:
            raise ValueError("detected results require a language code and top score")
    elif result.language_code is not None:
        raise ValueError("non-detected results cannot have a language code")


def _validate_non_linguistic_scores(result: LanguageResult) -> None:
    if result.status is not LanguageStatus.NON_LINGUISTIC:
        return
    if any(
        score is not None for score in (result.top_score, result.runner_up_score, result.margin)
    ):
        raise ValueError("non-linguistic results cannot have confidence scores")


def _validate_runner_up_score(result: LanguageResult) -> None:
    if result.runner_up_score is None:
        return
    if result.top_score is None:
        raise ValueError("runner-up score requires a top score")
    if result.runner_up_score > result.top_score:
        raise ValueError("runner-up score cannot exceed top score")


def _validate_margin(result: LanguageResult) -> None:
    if result.margin is None:
        return
    if result.top_score is None or result.runner_up_score is None:
        raise ValueError("margin requires top and runner-up scores")
    expected_margin = result.top_score - result.runner_up_score
    if not math.isclose(result.margin, expected_margin, rel_tol=0.0, abs_tol=1e-12):
        raise ValueError("margin must equal top score minus runner-up score")


@dataclass(frozen=True, slots=True)
class LanguageModelIdentity:
    """Pinned detector metadata, separate from each text result.

    ``config_fingerprint`` identifies this library/version, scope, accuracy
    mode, and policy configuration. ``binary_artifact_hash`` is intentionally
    ``None`` until a separately verified wheel/artifact digest is supplied; a
    configuration hash is not presented as a binary hash.
    """

    policy: LanguagePolicy
    language_scope: tuple[str, ...]
    library_name: str = field(init=False)
    library_version: str = field(init=False)
    policy_fingerprint: str = field(init=False)
    config_fingerprint: str = field(init=False)
    binary_artifact_hash: str | None = field(init=False)

    def __post_init__(self) -> None:
        if not isinstance(self.policy, LanguagePolicy):
            raise TypeError("policy must be a LanguagePolicy")
        scope = _validated_scope(self.language_scope)
        policy_payload = _policy_payload(self.policy)
        object.__setattr__(self, "language_scope", scope)
        object.__setattr__(self, "library_name", LINGUA_LIBRARY_NAME)
        object.__setattr__(self, "library_version", PINNED_LINGUA_VERSION)
        object.__setattr__(self, "policy_fingerprint", _sha256_json(policy_payload))
        object.__setattr__(
            self,
            "config_fingerprint",
            _sha256_json(
                {
                    "library_name": LINGUA_LIBRARY_NAME,
                    "library_version": PINNED_LINGUA_VERSION,
                    "accuracy_mode": "high_accuracy",
                    "language_scope": scope,
                    "policy": policy_payload,
                }
            ),
        )
        object.__setattr__(self, "binary_artifact_hash", None)


def _policy_payload(policy: LanguagePolicy) -> dict[str, object]:
    return {
        "min_alphabetic_chars": policy.min_alphabetic_chars,
        "min_score": policy.min_score,
        "min_margin": policy.min_margin,
        "tie_epsilon": policy.tie_epsilon,
    }


def _sha256_json(payload: object) -> str:
    # pragma: no mutate start - ensure_ascii=None equals False; exact fingerprints are tested
    encoded = json.dumps(
        payload,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode()
    # pragma: no mutate end
    return hashlib.sha256(encoded).hexdigest()


def _validated_scope(language_scope: tuple[str, ...]) -> tuple[str, ...]:
    scope = tuple(sorted({_validated_scope_item(item) for item in language_scope}))
    if not scope:
        raise ValueError("language_scope must contain non-empty strings")
    return scope


def _validated_scope_item(item: object) -> str:
    if not isinstance(item, str):
        raise TypeError("language_scope items must be strings")
    if not item:
        raise ValueError("language_scope items must be non-empty")
    return item


def language_model_identity(
    policy: LanguagePolicy,
    *,
    language_scope: tuple[str, ...] = DEFAULT_LANGUAGE_SCOPE,
) -> LanguageModelIdentity:
    """Build deterministic metadata for a pinned high-accuracy configuration."""
    return LanguageModelIdentity(policy=policy, language_scope=language_scope)


DEFAULT_LANGUAGE_POLICY = LanguagePolicy()


__all__ = [
    "DEFAULT_LANGUAGE_POLICY",
    "DEFAULT_LANGUAGE_SCOPE",
    "LINGUA_LIBRARY_NAME",
    "PINNED_LINGUA_VERSION",
    "LanguageModelIdentity",
    "LanguagePolicy",
    "LanguageResult",
    "LanguageStatus",
    "language_model_identity",
]
