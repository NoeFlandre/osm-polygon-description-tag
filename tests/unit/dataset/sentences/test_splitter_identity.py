"""The run identity must record which splitter produced its sentences.

``model_config_fingerprint`` already binds a run to its detector. Splitting now
happens in the same pass and changes what is published, so the splitter and the
exact set of languages it is allowed to split belong in that same binding: a run
prepared before this change must not accept parts produced after it.
"""

from __future__ import annotations

from osm_polygon_description_tag.dataset.languages.models import (
    DEFAULT_LANGUAGE_POLICY,
    LanguagePolicy,
    cascade_model_identity,
    language_model_identity,
)
from osm_polygon_description_tag.dataset.sentences import sat
from osm_polygon_description_tag.dataset.sentences.languages import (
    supported_languages_fingerprint,
)


def test_the_identity_names_the_pinned_splitter_and_its_supported_set() -> None:
    identity = language_model_identity(DEFAULT_LANGUAGE_POLICY)

    assert identity.splitter_name == sat.SPLITTER_NAME
    assert identity.splitter_revision == sat.SAT_MODEL_REVISION
    assert identity.splitter_languages_fingerprint == supported_languages_fingerprint()


def test_the_cascade_identity_records_the_same_splitter() -> None:
    """The splitter does not depend on which detector chose the language."""
    identity = cascade_model_identity(DEFAULT_LANGUAGE_POLICY)

    assert identity.splitter_name == sat.SPLITTER_NAME
    assert identity.splitter_revision == sat.SAT_MODEL_REVISION


def test_the_config_fingerprint_covers_the_splitter_revision(monkeypatch) -> None:
    """A different splitter artifact must produce a different run identity."""
    before = language_model_identity(DEFAULT_LANGUAGE_POLICY).config_fingerprint

    monkeypatch.setattr(sat, "SAT_MODEL_REVISION", "0" * 40)
    after = language_model_identity(DEFAULT_LANGUAGE_POLICY).config_fingerprint

    assert before != after


def test_the_config_fingerprint_covers_the_supported_language_set(monkeypatch) -> None:
    """Widening or narrowing what may be split changes what the run publishes."""
    from osm_polygon_description_tag.dataset.languages import models as models_module

    before = language_model_identity(DEFAULT_LANGUAGE_POLICY).config_fingerprint

    monkeypatch.setattr(models_module, "supported_languages_fingerprint", lambda: "f" * 64)
    after = language_model_identity(DEFAULT_LANGUAGE_POLICY).config_fingerprint

    assert before != after


def test_the_policy_still_changes_the_fingerprint_independently() -> None:
    """Adding the splitter must not have flattened the detector's own inputs."""
    other = LanguagePolicy(min_alphabetic_chars=DEFAULT_LANGUAGE_POLICY.min_alphabetic_chars + 1)

    assert (
        language_model_identity(DEFAULT_LANGUAGE_POLICY).config_fingerprint
        != language_model_identity(other).config_fingerprint
    )
