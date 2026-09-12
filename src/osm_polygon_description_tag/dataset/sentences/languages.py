"""Which detected languages the pinned sentence splitter is competent on.

SaT-3l-sm is a supervised mixture trained on 85 languages, and that list --- not
the model's willingness to accept any string --- is what "competent" means here.
The table below is the authority: it is pinned in source rather than read from
the installed library at runtime, so a library upgrade that quietly widens or
narrows the set changes a fingerprint instead of silently changing the dataset.

Two code systems meet at this boundary. The detector upstream emits ISO 639-3
(``eng``), because that is what both Lingua and GlotLID report; SaT names its
languages in ISO 639-1 (``en``), except Cebuano, which has no 639-1 code. The
table is therefore written in SaT's direction --- one SaT language to every
639-3 code that means it --- and inverted once, at import, into the lookup.

The aliases are macrolanguage members a pinned detector actually emits: Lingua
reports Norwegian as ``nob`` or ``nno`` rather than the macrolanguage ``nor``,
and GlotLID prefers individual codes such as ``arb`` over ``ara``. Anything not
in the table is unsupported, and unsupported means the description is left
unsplit rather than split by a model that was never trained on it.
"""

from __future__ import annotations

import hashlib
from collections.abc import Mapping
from typing import Final

from osm_polygon_description_tag.runtime.serialization import canonical_json_bytes

_ISO_639_3_BY_SAT_LANGUAGE: Final[Mapping[str, tuple[str, ...]]] = {
    "af": ("afr",),
    "am": ("amh",),
    "ar": (
        "ara",
        "arb",
    ),
    "az": (
        "aze",
        "azj",
    ),
    "be": ("bel",),
    "bg": ("bul",),
    "bn": ("ben",),
    "ca": ("cat",),
    "ceb": ("ceb",),
    "cs": ("ces",),
    "cy": ("cym",),
    "da": ("dan",),
    "de": ("deu",),
    "el": ("ell",),
    "en": ("eng",),
    "eo": ("epo",),
    "es": ("spa",),
    "et": (
        "est",
        "ekk",
    ),
    "eu": ("eus",),
    "fa": (
        "fas",
        "pes",
    ),
    "fi": ("fin",),
    "fr": ("fra",),
    "fy": ("fry",),
    "ga": ("gle",),
    "gd": ("gla",),
    "gl": ("glg",),
    "gu": ("guj",),
    "ha": ("hau",),
    "he": ("heb",),
    "hi": ("hin",),
    "hu": ("hun",),
    "hy": ("hye",),
    "id": ("ind",),
    "ig": ("ibo",),
    "is": ("isl",),
    "it": ("ita",),
    "ja": ("jpn",),
    "jv": ("jav",),
    "ka": ("kat",),
    "kk": ("kaz",),
    "km": ("khm",),
    "kn": ("kan",),
    "ko": ("kor",),
    "ku": (
        "kur",
        "kmr",
    ),
    "ky": ("kir",),
    "la": ("lat",),
    "lt": ("lit",),
    "lv": (
        "lav",
        "lvs",
    ),
    "mg": (
        "mlg",
        "plt",
    ),
    "mk": ("mkd",),
    "ml": ("mal",),
    "mn": ("mon",),
    "mr": ("mar",),
    "ms": (
        "msa",
        "zsm",
    ),
    "mt": ("mlt",),
    "my": ("mya",),
    "ne": (
        "nep",
        "npi",
    ),
    "nl": ("nld",),
    "no": (
        "nor",
        "nob",
        "nno",
    ),
    "pa": ("pan",),
    "pl": ("pol",),
    "ps": ("pus",),
    "pt": ("por",),
    "ro": ("ron",),
    "ru": ("rus",),
    "si": ("sin",),
    "sk": ("slk",),
    "sl": ("slv",),
    "sq": (
        "sqi",
        "als",
    ),
    "sr": ("srp",),
    "sv": ("swe",),
    "ta": ("tam",),
    "te": ("tel",),
    "tg": ("tgk",),
    "th": ("tha",),
    "tr": ("tur",),
    "uk": ("ukr",),
    "ur": ("urd",),
    "uz": (
        "uzb",
        "uzn",
    ),
    "vi": ("vie",),
    "xh": ("xho",),
    "yi": ("yid",),
    "yo": ("yor",),
    "zh": (
        "zho",
        "cmn",
    ),
    "zu": ("zul",),
}

SAT_SUPPORTED_LANGUAGES: Final[frozenset[str]] = frozenset(_ISO_639_3_BY_SAT_LANGUAGE)
SAT_LANGUAGE_COUNT: Final[int] = len(_ISO_639_3_BY_SAT_LANGUAGE)


def _inverted(table: Mapping[str, tuple[str, ...]]) -> dict[str, str]:
    """Invert the table, refusing a 639-3 code claimed by two SaT languages."""
    lookup: dict[str, str] = {}
    for sat_language, iso_codes in table.items():
        for iso_code in iso_codes:
            if iso_code in lookup:
                raise ValueError(f"ISO 639-3 code claimed twice: {iso_code}")
            lookup[iso_code] = sat_language
    return lookup


_SAT_LANGUAGE_BY_ISO_639_3: Final[Mapping[str, str]] = _inverted(_ISO_639_3_BY_SAT_LANGUAGE)


def sat_language_for(language_code: object) -> str | None:
    """Return the SaT language for a detected ISO 639-3 code, or ``None``.

    ``None`` means "not supported": the caller must leave the description
    unsplit rather than fall back to any other language.
    """
    if not isinstance(language_code, str):
        return None
    return _SAT_LANGUAGE_BY_ISO_639_3.get(language_code)


def supported_languages_fingerprint() -> str:
    """Return the digest that binds a run to this exact supported-language set."""
    canonical = canonical_json_bytes(
        {code: list(_ISO_639_3_BY_SAT_LANGUAGE[code]) for code in sorted(SAT_SUPPORTED_LANGUAGES)}
    )
    return hashlib.sha256(canonical).hexdigest()


__all__ = [
    "SAT_LANGUAGE_COUNT",
    "SAT_SUPPORTED_LANGUAGES",
    "sat_language_for",
    "supported_languages_fingerprint",
]
