"""Exact refusals and exact pinned values of the detector identity metadata.

``model_config_fingerprint`` is written into every annotation row and into the
snapshot, so a wrong or missing library name silently changes the provenance of
a whole run. These tests pin the values themselves, not their presence.
"""

from typing import Any

import pytest

from osm_polygon_description_tag.dataset.languages.models import (
    DEFAULT_LANGUAGE_SCOPE,
    GLOTLID_LIBRARY_NAME,
    GLOTLID_LIBRARY_VERSION,
    GLOTLID_MODEL_FILENAME,
    GLOTLID_MODEL_REPOSITORY,
    GLOTLID_MODEL_REVISION,
    GLOTLID_MODEL_SHA256,
    GLOTLID_RUNTIME_LIBRARY_NAME,
    GLOTLID_RUNTIME_LIBRARY_VERSION,
    LanguageModelIdentity,
    LanguagePolicy,
    cascade_model_identity,
    glotlid_model_identity,
    language_model_identity,
)

_GLOTLID_METADATA: dict[str, Any] = {
    "model_repository": GLOTLID_MODEL_REPOSITORY,
    "model_filename": GLOTLID_MODEL_FILENAME,
    "model_revision": GLOTLID_MODEL_REVISION,
    "runtime_library_name": GLOTLID_RUNTIME_LIBRARY_NAME,
    "runtime_library_version": GLOTLID_RUNTIME_LIBRARY_VERSION,
}


def _policy() -> LanguagePolicy:
    return LanguagePolicy()


def test_a_lingua_identity_carrying_external_metadata_is_refused_exactly() -> None:
    with pytest.raises(ValueError) as caught:
        LanguageModelIdentity(
            policy=_policy(),
            language_scope=DEFAULT_LANGUAGE_SCOPE,
            model_repository=GLOTLID_MODEL_REPOSITORY,
        )

    assert str(caught.value) == "Lingua identity must not contain external model metadata"


@pytest.mark.parametrize("dropped", sorted(_GLOTLID_METADATA))
def test_a_glotlid_identity_missing_one_pinned_field_is_refused_exactly(dropped: str) -> None:
    metadata = {key: value for key, value in _GLOTLID_METADATA.items() if key != dropped}

    with pytest.raises(ValueError) as caught:
        LanguageModelIdentity(
            policy=_policy(),
            language_scope=DEFAULT_LANGUAGE_SCOPE,
            detector_name=glotlid_model_identity(_policy()).detector_name,
            **metadata,
        )

    assert (
        str(caught.value) == "GlotLID identity metadata is not pinned to the supported v3 artifact"
    )


def test_the_glotlid_identity_records_the_pinned_library_and_artifact() -> None:
    identity = glotlid_model_identity(_policy())

    assert identity.library_name == GLOTLID_LIBRARY_NAME
    assert identity.library_version == GLOTLID_LIBRARY_VERSION
    assert identity.binary_artifact_hash == GLOTLID_MODEL_SHA256
    assert identity.model_repository == GLOTLID_MODEL_REPOSITORY
    assert identity.model_revision == GLOTLID_MODEL_REVISION
    assert identity.runtime_library_name == GLOTLID_RUNTIME_LIBRARY_NAME
    assert identity.runtime_library_version == GLOTLID_RUNTIME_LIBRARY_VERSION


def test_the_pinned_glotlid_constants_are_the_documented_v3_artifact() -> None:
    """These exact strings are what the runbook and the job script verify."""
    assert GLOTLID_LIBRARY_NAME == "GlotLID"
    assert GLOTLID_LIBRARY_VERSION == "v3"
    assert GLOTLID_MODEL_REPOSITORY == "cis-lmu/glotlid"
    assert GLOTLID_MODEL_FILENAME == "model_v3.bin"
    assert GLOTLID_MODEL_REVISION == "85cd6716494360367b75f642b5bc78667605d0b4"
    assert GLOTLID_MODEL_SHA256 == (
        "a818b6bd42a628ab47d3dfc1578c7ea615c45381f3494c42535e31e8c4cafc9e"
    )
    assert GLOTLID_RUNTIME_LIBRARY_NAME == "fasttext-numpy2"
    assert GLOTLID_RUNTIME_LIBRARY_VERSION == "0.10.2"


def test_a_lingua_identity_leaves_every_external_field_unset() -> None:
    identity = language_model_identity(_policy())

    assert identity.model_repository is None
    assert identity.model_filename is None
    assert identity.model_revision is None
    assert identity.runtime_library_name is None
    assert identity.runtime_library_version is None
    assert identity.binary_artifact_hash is None


def test_the_cascade_identity_keeps_lingua_as_its_library_and_glotlid_as_its_artifact() -> None:
    cascade = cascade_model_identity(_policy())
    primary = language_model_identity(_policy())

    assert cascade.library_name == primary.library_name
    assert cascade.library_version == primary.library_version
    assert cascade.binary_artifact_hash == GLOTLID_MODEL_SHA256
    assert cascade.config_fingerprint != primary.config_fingerprint
    assert cascade.config_fingerprint != glotlid_model_identity(_policy()).config_fingerprint
