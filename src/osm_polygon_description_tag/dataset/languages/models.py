"""Immutable contracts for conservative language detection."""

import hashlib
import json
import math
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Final

from osm_polygon_description_tag.dataset.sentences import sat
from osm_polygon_description_tag.dataset.sentences.languages import (
    supported_languages_fingerprint,
)

LINGUA_LIBRARY_NAME: Final = "lingua-language-detector"
PINNED_LINGUA_VERSION: Final = "2.2.0"
DEFAULT_LANGUAGE_SCOPE: Final = ("all_supported",)
LINGUA_DETECTOR_NAME: Final = "lingua"
GLOTLID_DETECTOR_NAME: Final = "glotlid-v3"
CASCADE_DETECTOR_NAME: Final = "lingua+glotlid-v3-fallback"
GLOTLID_LIBRARY_NAME: Final = "GlotLID"
GLOTLID_LIBRARY_VERSION: Final = "v3"
GLOTLID_MODEL_REPOSITORY: Final = "cis-lmu/glotlid"
GLOTLID_MODEL_FILENAME: Final = "model_v3.bin"
GLOTLID_MODEL_REVISION: Final = "85cd6716494360367b75f642b5bc78667605d0b4"
GLOTLID_MODEL_SHA256: Final = "a818b6bd42a628ab47d3dfc1578c7ea615c45381f3494c42535e31e8c4cafc9e"
GLOTLID_RUNTIME_LIBRARY_NAME: Final = "fasttext-numpy2"
GLOTLID_RUNTIME_LIBRARY_VERSION: Final = "0.10.2"


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


def _splitter_payload() -> dict[str, object]:
    """Return the pinned splitter's contribution to a run's identity.

    Splitting happens in the same pass as detection and changes what a run
    publishes, so the splitter artifact and the exact set of languages it is
    allowed to split are part of the run's identity, not metadata beside it.
    """
    return {
        "splitter_name": sat.SPLITTER_NAME,
        "splitter_repository": sat.SAT_MODEL_REPOSITORY,
        "splitter_revision": sat.SAT_MODEL_REVISION,
        "splitter_artifact_hash": sat.SAT_MODEL_SHA256,
        "splitter_runtime_library_name": sat.SAT_RUNTIME_LIBRARY_NAME,
        "splitter_runtime_library_version": sat.SAT_RUNTIME_LIBRARY_VERSION,
        "splitter_languages_fingerprint": supported_languages_fingerprint(),
    }


@dataclass(frozen=True, slots=True)
class LanguageModelIdentity:
    """Pinned detector metadata, separate from each text result.

    ``config_fingerprint`` identifies the primary library, optional fallback,
    scope, accuracy mode, and policy configuration. The optional external
    fields identify the pinned fallback artifact; they are empty for Lingua.
    """

    policy: LanguagePolicy
    language_scope: tuple[str, ...]
    detector_name: str = LINGUA_DETECTOR_NAME
    model_repository: str | None = None
    model_filename: str | None = None
    model_revision: str | None = None
    runtime_library_name: str | None = None
    runtime_library_version: str | None = None
    library_name: str = field(init=False)
    library_version: str = field(init=False)
    policy_fingerprint: str = field(init=False)
    config_fingerprint: str = field(init=False)
    binary_artifact_hash: str | None = field(init=False)
    splitter_name: str = field(init=False)
    splitter_revision: str = field(init=False)
    splitter_languages_fingerprint: str = field(init=False)

    def __post_init__(self) -> None:
        if not isinstance(self.policy, LanguagePolicy):
            raise TypeError("policy must be a LanguagePolicy")
        scope = _validated_scope(self.language_scope)
        policy_payload = _policy_payload(self.policy)
        object.__setattr__(self, "language_scope", scope)
        object.__setattr__(self, "policy_fingerprint", _sha256_json(policy_payload))
        object.__setattr__(self, "splitter_name", sat.SPLITTER_NAME)
        object.__setattr__(self, "splitter_revision", sat.SAT_MODEL_REVISION)
        object.__setattr__(
            self, "splitter_languages_fingerprint", supported_languages_fingerprint()
        )
        if self.detector_name == LINGUA_DETECTOR_NAME:
            _set_lingua_identity(self, scope, policy_payload)
        elif self.detector_name == GLOTLID_DETECTOR_NAME:
            _set_glotlid_identity(self, scope, policy_payload)
        elif self.detector_name == CASCADE_DETECTOR_NAME:
            _set_cascade_identity(self, scope, policy_payload)
        else:
            raise ValueError(f"unsupported detector_name: {self.detector_name!r}")


def _external_metadata(identity: LanguageModelIdentity) -> tuple[str | None, ...]:
    return (
        identity.model_repository,
        identity.model_filename,
        identity.model_revision,
        identity.runtime_library_name,
        identity.runtime_library_version,
    )


def _require_lingua_metadata(identity: LanguageModelIdentity) -> None:
    if any(value is not None for value in _external_metadata(identity)):
        raise ValueError("Lingua identity must not contain external model metadata")


def _require_glotlid_metadata(identity: LanguageModelIdentity) -> None:
    expected = (
        GLOTLID_MODEL_REPOSITORY,
        GLOTLID_MODEL_FILENAME,
        GLOTLID_MODEL_REVISION,
        GLOTLID_RUNTIME_LIBRARY_NAME,
        GLOTLID_RUNTIME_LIBRARY_VERSION,
    )
    if _external_metadata(identity) != expected:
        raise ValueError("GlotLID identity metadata is not pinned to the supported v3 artifact")


def _set_lingua_identity(
    identity: LanguageModelIdentity,
    scope: tuple[str, ...],
    policy_payload: dict[str, object],
) -> None:
    _require_lingua_metadata(identity)
    object.__setattr__(identity, "library_name", LINGUA_LIBRARY_NAME)
    object.__setattr__(identity, "library_version", PINNED_LINGUA_VERSION)
    object.__setattr__(identity, "binary_artifact_hash", None)
    object.__setattr__(
        identity,
        "config_fingerprint",
        _sha256_json(
            {
                "library_name": LINGUA_LIBRARY_NAME,
                "library_version": PINNED_LINGUA_VERSION,
                "accuracy_mode": "high_accuracy",
                "language_scope": scope,
                "policy": policy_payload,
                **_splitter_payload(),
            }
        ),
    )


def _set_glotlid_identity(
    identity: LanguageModelIdentity,
    scope: tuple[str, ...],
    policy_payload: dict[str, object],
) -> None:
    _require_glotlid_metadata(identity)
    object.__setattr__(identity, "library_name", GLOTLID_LIBRARY_NAME)
    object.__setattr__(identity, "library_version", GLOTLID_LIBRARY_VERSION)
    object.__setattr__(identity, "binary_artifact_hash", GLOTLID_MODEL_SHA256)
    object.__setattr__(identity, "config_fingerprint", _glotlid_fingerprint(scope, policy_payload))


def _set_cascade_identity(
    identity: LanguageModelIdentity,
    scope: tuple[str, ...],
    policy_payload: dict[str, object],
) -> None:
    _require_glotlid_metadata(identity)
    object.__setattr__(identity, "library_name", LINGUA_LIBRARY_NAME)
    object.__setattr__(identity, "library_version", PINNED_LINGUA_VERSION)
    object.__setattr__(identity, "binary_artifact_hash", GLOTLID_MODEL_SHA256)
    object.__setattr__(identity, "config_fingerprint", _cascade_fingerprint(scope, policy_payload))


def _glotlid_fingerprint(scope: tuple[str, ...], policy: dict[str, object]) -> str:
    return _sha256_json(
        {
            "detector_name": GLOTLID_DETECTOR_NAME,
            "library_name": GLOTLID_LIBRARY_NAME,
            "library_version": GLOTLID_LIBRARY_VERSION,
            "model_repository": GLOTLID_MODEL_REPOSITORY,
            "model_filename": GLOTLID_MODEL_FILENAME,
            "model_revision": GLOTLID_MODEL_REVISION,
            "runtime_library_name": GLOTLID_RUNTIME_LIBRARY_NAME,
            "runtime_library_version": GLOTLID_RUNTIME_LIBRARY_VERSION,
            "binary_artifact_hash": GLOTLID_MODEL_SHA256,
            "language_scope": scope,
            "policy": policy,
            **_splitter_payload(),
        }
    )


def _cascade_fingerprint(scope: tuple[str, ...], policy: dict[str, object]) -> str:
    return _sha256_json(
        {
            "detector_name": CASCADE_DETECTOR_NAME,
            "primary_library_name": LINGUA_LIBRARY_NAME,
            "primary_library_version": PINNED_LINGUA_VERSION,
            "fallback": {
                "library_name": GLOTLID_LIBRARY_NAME,
                "library_version": GLOTLID_LIBRARY_VERSION,
                "model_repository": GLOTLID_MODEL_REPOSITORY,
                "model_filename": GLOTLID_MODEL_FILENAME,
                "model_revision": GLOTLID_MODEL_REVISION,
                "runtime_library_name": GLOTLID_RUNTIME_LIBRARY_NAME,
                "runtime_library_version": GLOTLID_RUNTIME_LIBRARY_VERSION,
                "binary_artifact_hash": GLOTLID_MODEL_SHA256,
            },
            "accuracy_mode": "high_accuracy",
            "language_scope": scope,
            "policy": policy,
            **_splitter_payload(),
        }
    )


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


def glotlid_model_identity(
    policy: LanguagePolicy,
    *,
    language_scope: tuple[str, ...] = DEFAULT_LANGUAGE_SCOPE,
) -> LanguageModelIdentity:
    """Build deterministic metadata for the pinned GlotLID v3 artifact."""
    return LanguageModelIdentity(
        policy=policy,
        language_scope=language_scope,
        detector_name=GLOTLID_DETECTOR_NAME,
        model_repository=GLOTLID_MODEL_REPOSITORY,
        model_filename=GLOTLID_MODEL_FILENAME,
        model_revision=GLOTLID_MODEL_REVISION,
        runtime_library_name=GLOTLID_RUNTIME_LIBRARY_NAME,
        runtime_library_version=GLOTLID_RUNTIME_LIBRARY_VERSION,
    )


def cascade_model_identity(
    policy: LanguagePolicy,
    *,
    language_scope: tuple[str, ...] = DEFAULT_LANGUAGE_SCOPE,
) -> LanguageModelIdentity:
    """Build metadata for Lingua 2.2 with the pinned GlotLID v3 fallback."""
    return LanguageModelIdentity(
        policy=policy,
        language_scope=language_scope,
        detector_name=CASCADE_DETECTOR_NAME,
        model_repository=GLOTLID_MODEL_REPOSITORY,
        model_filename=GLOTLID_MODEL_FILENAME,
        model_revision=GLOTLID_MODEL_REVISION,
        runtime_library_name=GLOTLID_RUNTIME_LIBRARY_NAME,
        runtime_library_version=GLOTLID_RUNTIME_LIBRARY_VERSION,
    )


DEFAULT_LANGUAGE_POLICY = LanguagePolicy()
V2_LANGUAGE_POLICY: Final = LanguagePolicy(min_score=0.70)


__all__ = [
    "CASCADE_DETECTOR_NAME",
    "DEFAULT_LANGUAGE_POLICY",
    "DEFAULT_LANGUAGE_SCOPE",
    "GLOTLID_DETECTOR_NAME",
    "GLOTLID_LIBRARY_NAME",
    "GLOTLID_LIBRARY_VERSION",
    "GLOTLID_MODEL_FILENAME",
    "GLOTLID_MODEL_REPOSITORY",
    "GLOTLID_MODEL_REVISION",
    "GLOTLID_MODEL_SHA256",
    "GLOTLID_RUNTIME_LIBRARY_NAME",
    "GLOTLID_RUNTIME_LIBRARY_VERSION",
    "LINGUA_DETECTOR_NAME",
    "LINGUA_LIBRARY_NAME",
    "PINNED_LINGUA_VERSION",
    "V2_LANGUAGE_POLICY",
    "LanguageModelIdentity",
    "LanguagePolicy",
    "LanguageResult",
    "LanguageStatus",
    "cascade_model_identity",
    "glotlid_model_identity",
    "language_model_identity",
]
