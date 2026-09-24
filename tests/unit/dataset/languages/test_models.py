"""Validation of the immutable language contracts."""

import pytest

from osm_polygon_description_tag.dataset.languages.models import (
    CASCADE_DETECTOR_NAME,
    DEFAULT_LANGUAGE_POLICY,
    GLOTLID_MODEL_FILENAME,
    GLOTLID_MODEL_REPOSITORY,
    GLOTLID_MODEL_REVISION,
    GLOTLID_MODEL_SHA256,
    LanguageModelIdentity,
    LanguagePolicy,
    LanguageResult,
    LanguageStatus,
    cascade_model_identity,
    glotlid_model_identity,
    language_model_identity,
)
from tests.helpers.messages import exactly


def _detected(**overrides: object) -> LanguageResult:
    defaults: dict[str, object] = {
        "language_code": "eng",
        "top_score": 0.9,
        "runner_up_score": 0.1,
        "margin": 0.8,
        "status": LanguageStatus.DETECTED,
        "reason": "detected",
    }
    return LanguageResult(**{**defaults, **overrides})  # type: ignore[arg-type]


@pytest.mark.parametrize("value", ["high", None, [0.9]])
def test_a_non_numeric_threshold_is_rejected(value: object) -> None:
    with pytest.raises(TypeError, match="must be a real number"):
        LanguagePolicy(tie_epsilon=value)  # type: ignore[arg-type]


def test_a_boolean_threshold_is_rejected() -> None:
    with pytest.raises(TypeError, match="must be a real number"):
        LanguagePolicy(tie_epsilon=True)  # type: ignore[arg-type]


@pytest.mark.parametrize("value", [-0.1, 1.5, float("nan"), float("inf")])
def test_an_out_of_range_threshold_is_rejected(value: float) -> None:
    with pytest.raises(
        ValueError, match=exactly("tie_epsilon must be finite and between 0 and 1.0")
    ):
        LanguagePolicy(tie_epsilon=value)


@pytest.mark.parametrize(
    ("value", "error", "message"),
    [
        (0, ValueError, "min_alphabetic_chars must be positive"),
        (-1, ValueError, "min_alphabetic_chars must be positive"),
        (True, TypeError, "min_alphabetic_chars must be an integer"),
        (5.0, TypeError, "min_alphabetic_chars must be an integer"),
    ],
)
def test_an_invalid_minimum_length_is_rejected(
    value: object, error: type[Exception], message: str
) -> None:
    with pytest.raises(error) as caught:
        LanguagePolicy(min_alphabetic_chars=value)  # type: ignore[arg-type]
    assert str(caught.value) == message


def test_policy_accepts_inclusive_minimum_and_threshold_boundaries() -> None:
    policy = LanguagePolicy(min_alphabetic_chars=1, tie_epsilon=1.0)

    assert policy.min_alphabetic_chars == 1
    assert policy.tie_epsilon == 1.0


def test_the_policy_carries_no_confidence_threshold() -> None:
    """A score or margin gate was removed; only structural gates remain."""
    assert not hasattr(DEFAULT_LANGUAGE_POLICY, "min_score")
    assert not hasattr(DEFAULT_LANGUAGE_POLICY, "min_margin")
    assert DEFAULT_LANGUAGE_POLICY.min_alphabetic_chars == 5


def test_a_status_may_be_given_by_its_string_spelling() -> None:
    assert _detected(status="detected").status is LanguageStatus.DETECTED
    assert (
        _detected(
            language_code=None,
            top_score=None,
            runner_up_score=None,
            margin=None,
            status="non_linguistic",
            reason="no_letters",
        ).status
        is LanguageStatus.NON_LINGUISTIC
    )


def test_an_unknown_status_is_rejected() -> None:
    with pytest.raises(ValueError, match="is not a valid LanguageStatus"):
        _detected(status="invented")


@pytest.mark.parametrize("reason", ["", 7, None])
def test_a_result_requires_a_non_empty_reason(reason: object) -> None:
    with pytest.raises(ValueError) as caught:
        _detected(reason=reason)
    assert str(caught.value) == "reason must be a non-empty string"


def test_a_runner_up_score_requires_a_top_score() -> None:
    with pytest.raises(ValueError) as caught:
        LanguageResult(None, None, 0.2, None, LanguageStatus.UNCERTAIN, "odd")
    assert str(caught.value) == "runner-up score requires a top score"


def test_a_margin_requires_both_scores() -> None:
    with pytest.raises(ValueError) as caught:
        LanguageResult(None, 0.9, None, 0.4, LanguageStatus.UNCERTAIN, "odd")
    assert str(caught.value) == "margin requires top and runner-up scores"


def test_a_margin_must_equal_the_score_difference() -> None:
    with pytest.raises(ValueError) as caught:
        _detected(margin=0.5)
    assert str(caught.value) == "margin must equal top score minus runner-up score"


def test_a_runner_up_may_not_exceed_the_top_score() -> None:
    with pytest.raises(ValueError) as caught:
        LanguageResult(None, 0.4, 0.9, None, LanguageStatus.UNCERTAIN, "odd")
    assert str(caught.value) == "runner-up score cannot exceed top score"


def test_a_detected_result_requires_a_code_and_a_score() -> None:
    with pytest.raises(ValueError) as caught:
        LanguageResult(None, 0.9, None, None, LanguageStatus.DETECTED, "detected")
    assert str(caught.value) == "detected results require a language code and top score"


def test_a_non_detected_result_may_not_name_a_language() -> None:
    with pytest.raises(ValueError) as caught:
        LanguageResult("eng", 0.4, None, None, LanguageStatus.UNCERTAIN, "low_confidence")
    assert str(caught.value) == "non-detected results cannot have a language code"


def test_a_non_linguistic_result_may_not_carry_scores() -> None:
    with pytest.raises(ValueError) as caught:
        LanguageResult(None, 0.4, None, None, LanguageStatus.NON_LINGUISTIC, "no_letters")
    assert str(caught.value) == "non-linguistic results cannot have confidence scores"


def test_result_scores_must_remain_within_the_probability_range() -> None:
    with pytest.raises(ValueError) as caught:
        LanguageResult("eng", 1.5, None, None, LanguageStatus.DETECTED, "detected")
    assert str(caught.value) == "top_score must be finite and between 0 and 1.0"
    with pytest.raises(ValueError):
        LanguageResult(None, 1.5, 1.5, None, LanguageStatus.UNCERTAIN, "tie")
    with pytest.raises(ValueError):
        LanguageResult(None, 1.5, 0.0, 1.5, LanguageStatus.UNCERTAIN, "score")


def test_uncertain_ties_allow_equal_runner_up_scores() -> None:
    result = LanguageResult(None, 0.9, 0.9, 0.0, LanguageStatus.UNCERTAIN, "tie")

    assert result.status is LanguageStatus.UNCERTAIN
    assert result.runner_up_score == result.top_score


def test_margin_validation_uses_the_explicit_absolute_tolerance() -> None:
    accepted = LanguageResult(
        "eng", 0.75, 0.5, 0.2500000000005, LanguageStatus.DETECTED, "detected"
    )
    assert accepted.margin == 0.2500000000005
    with pytest.raises(ValueError):
        LanguageResult("eng", 0.9, 0.1, 0.8000000005, LanguageStatus.DETECTED, "detected")


@pytest.mark.parametrize("code", ["en", "ENG", "en1", "engl", 7, ""])
def test_a_language_code_must_be_lowercase_iso_639_3(code: object) -> None:
    with pytest.raises(ValueError) as caught:
        _detected(language_code=code)
    assert str(caught.value) == "language_code must be a lowercase ISO 639-3 code"


def test_an_identity_requires_a_policy() -> None:
    with pytest.raises(TypeError, match=exactly("policy must be a LanguagePolicy")):
        LanguageModelIdentity(policy="strict", language_scope=("all_supported",))  # type: ignore[arg-type]


def test_a_scope_must_contain_non_empty_strings() -> None:
    with pytest.raises(ValueError) as caught:
        language_model_identity(LanguagePolicy(), language_scope=())
    assert str(caught.value) == "language_scope must contain non-empty strings"
    with pytest.raises(TypeError) as caught:
        language_model_identity(LanguagePolicy(), language_scope=(7,))  # type: ignore[arg-type]
    assert str(caught.value) == "language_scope items must be strings"
    with pytest.raises(ValueError) as caught:
        language_model_identity(LanguagePolicy(), language_scope=("",))
    assert str(caught.value) == "language_scope items must be non-empty"


def test_a_scope_is_deduplicated_and_ordered() -> None:
    identity = language_model_identity(LanguagePolicy(), language_scope=("fra", "eng", "fra"))

    assert identity.language_scope == ("eng", "fra")


def test_fingerprints_track_the_configuration() -> None:
    default = language_model_identity(LanguagePolicy())
    strict = language_model_identity(LanguagePolicy(min_alphabetic_chars=9))
    scoped = language_model_identity(LanguagePolicy(), language_scope=("eng",))

    assert default.policy_fingerprint != strict.policy_fingerprint
    assert default.config_fingerprint != strict.config_fingerprint
    assert default.config_fingerprint != scoped.config_fingerprint
    assert default.policy_fingerprint == scoped.policy_fingerprint
    assert default.binary_artifact_hash is None
    assert default.library_version == "2.2.0"


def test_cascade_identity_records_the_pinned_glotlid_fallback() -> None:
    primary = language_model_identity(DEFAULT_LANGUAGE_POLICY)
    cascade = cascade_model_identity(DEFAULT_LANGUAGE_POLICY)

    assert cascade.detector_name == CASCADE_DETECTOR_NAME
    assert cascade.library_name == primary.library_name
    assert cascade.library_version == primary.library_version
    assert cascade.model_repository == GLOTLID_MODEL_REPOSITORY
    assert cascade.model_filename == GLOTLID_MODEL_FILENAME
    assert cascade.model_revision == GLOTLID_MODEL_REVISION
    assert cascade.binary_artifact_hash == GLOTLID_MODEL_SHA256
    assert cascade.config_fingerprint != primary.config_fingerprint


def test_fallback_identity_fingerprints_are_pinned_exactly() -> None:
    """Every field of the GlotLID and cascade payloads is provenance-bearing.

    These fingerprints are written into each annotation row and bind the
    snapshot, so a renamed key or a swapped constant must change the digest
    rather than silently keep the old provenance.
    """
    glotlid = glotlid_model_identity(LanguagePolicy())
    glotlid_unicode = glotlid_model_identity(LanguagePolicy(), language_scope=("é",))
    cascade = cascade_model_identity(LanguagePolicy())
    cascade_unicode = cascade_model_identity(LanguagePolicy(), language_scope=("é",))
    cascade_variant = cascade_model_identity(LanguagePolicy(min_alphabetic_chars=7))

    assert glotlid.config_fingerprint == (
        "fdef129c5596c2b24d1d96166c1cfcbf2198ce576bc9f7ade5f07f46202270eb"
    )
    assert glotlid_unicode.config_fingerprint == (
        "367996be3160bc9f800f6c550f4e4dfd5a30f92d798ce130ad16f091770d41cb"
    )
    assert cascade.config_fingerprint == (
        "1d6f31e245a922d89d6d341f24cf0db2304160b2b7ce4f5d8eb890eef148c9a7"
    )
    assert cascade_unicode.config_fingerprint == (
        "8cca62e3469d615d815ce9bc9868b563a1a92aa71704b118415e4e1de0a1ea5e"
    )
    assert cascade_variant.config_fingerprint == (
        "8ec9d6748e5c750d0f617aab800a1c0083daf3f3631470d4fc6c578830c2d749"
    )
    assert cascade_variant.policy_fingerprint == (
        "a1158f6df6db870156f52234dcca39d32e69513a84d567cfd3be3df0cd974289"
    )


def test_identity_fingerprints_use_canonical_and_unicode_safe_serialization() -> None:
    default = language_model_identity(LanguagePolicy())
    unicode_scope = language_model_identity(LanguagePolicy(), language_scope=("é",))

    assert default.policy_fingerprint == (
        "d9d4a96aeed0cf08282bcb22f7e83b80531b3459eb0c5bbc9ec24ea76eead107"
    )
    assert default.config_fingerprint == (
        "43f31727f50bf00d92986a122fd68919e19f6a709c20551856bf816d420ec13f"
    )
    assert unicode_scope.config_fingerprint == (
        "2ae1ae6f1e949ae52e56964fa1ca3ff6a9e0b0c0eb242355aec05d0531e6b262"
    )
