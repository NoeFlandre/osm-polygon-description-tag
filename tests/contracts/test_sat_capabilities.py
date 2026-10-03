"""Independent offline contracts for the shared SaT capability reference."""

from __future__ import annotations

import hashlib
import json
from importlib.resources import files

import pytest

from osm_polygon_description_tag.dataset.sentences.sat_capabilities import (
    REFERENCE_SHA256,
    REFERENCE_VERSION,
    load_supported_languages,
    parse_supported_languages,
)

EXPECTED_CODES = (
    "af",
    "am",
    "ar",
    "az",
    "be",
    "bg",
    "bn",
    "ca",
    "ceb",
    "cs",
    "cy",
    "da",
    "de",
    "el",
    "en",
    "eo",
    "es",
    "et",
    "eu",
    "fa",
    "fi",
    "fr",
    "fy",
    "ga",
    "gd",
    "gl",
    "gu",
    "ha",
    "he",
    "hi",
    "hu",
    "hy",
    "id",
    "ig",
    "is",
    "it",
    "ja",
    "jv",
    "ka",
    "kk",
    "km",
    "kn",
    "ko",
    "ku",
    "ky",
    "la",
    "lt",
    "lv",
    "mg",
    "mk",
    "ml",
    "mn",
    "mr",
    "ms",
    "mt",
    "my",
    "ne",
    "nl",
    "no",
    "pa",
    "pl",
    "ps",
    "pt",
    "ro",
    "ru",
    "si",
    "sk",
    "sl",
    "sq",
    "sr",
    "sv",
    "ta",
    "te",
    "tg",
    "th",
    "tr",
    "uk",
    "ur",
    "uz",
    "vi",
    "xh",
    "yi",
    "yo",
    "zh",
    "zu",
)


def test_reference_keeps_the_original_85_codes() -> None:
    assert load_supported_languages() == EXPECTED_CODES
    assert REFERENCE_VERSION == "sat-3l-sm-v1"


def test_packaged_reference_has_valid_provenance_and_digests() -> None:
    content = (
        files("osm_polygon_description_tag").joinpath("_data/sat-capabilities.json").read_bytes()
    )
    assert hashlib.sha256(content).hexdigest() == REFERENCE_SHA256
    reference = json.loads(content)
    digest = reference.pop("digest")
    canonical = json.dumps(reference, sort_keys=True, separators=(",", ":")).encode()
    assert digest == "sha256:" + hashlib.sha256(canonical).hexdigest()
    assert reference["reference_version"] == REFERENCE_VERSION
    assert reference["model_id"] == "segment-any-text/sat-3l-sm"
    assert reference["model_revision"] == "137da054051ad9f1eac42025f758db4ac9f22535"
    assert reference["authority"] == "NoeFlandre/osm-polygon-description-tag"
    assert reference["schema_version"] == 1
    assert reference["supported_languages"] == list(EXPECTED_CODES)
    assert parse_supported_languages(content) == EXPECTED_CODES


@pytest.mark.parametrize("content", [b"", b"{}", b"invalid", b"[]"])
def test_reference_loader_fails_closed_for_unpinned_bytes(content: bytes) -> None:
    with pytest.raises(ValueError, match=r"^SaT capability reference digest mismatch$"):
        parse_supported_languages(content)


def test_a_valid_json_reference_with_one_changed_code_is_rejected() -> None:
    content = (
        files("osm_polygon_description_tag").joinpath("_data/sat-capabilities.json").read_bytes()
    )
    changed = content.replace(b'"en"', b'"xx"')
    with pytest.raises(ValueError, match=r"^SaT capability reference digest mismatch$"):
        parse_supported_languages(changed)


def test_description_policy_and_historical_fingerprint_are_unchanged() -> None:
    from osm_polygon_description_tag.dataset.sentences.languages import (
        SAT_SUPPORTED_LANGUAGES,
        sat_language_for,
        supported_languages_fingerprint,
    )

    assert frozenset(EXPECTED_CODES) == SAT_SUPPORTED_LANGUAGES
    assert supported_languages_fingerprint() == (
        "afdd2bd7aaf106d61b7a692ac0742ddedf33457eb48f24410f5081ac7062809b"
    )
    for language in ("ckb", "khk", "pbt", "ydd", "yue", "eng_Latn", "en"):
        assert sat_language_for(language) is None
    assert sat_language_for("eng") == "en"


def test_description_aliases_cannot_bypass_the_reference(monkeypatch) -> None:
    from osm_polygon_description_tag.dataset.sentences import languages

    monkeypatch.setattr(languages, "SAT_SUPPORTED_LANGUAGES", frozenset())
    assert languages.sat_language_for("eng") is None
