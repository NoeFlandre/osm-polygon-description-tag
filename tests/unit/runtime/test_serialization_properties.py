"""Canonical JSON: round trip, key-order independence and digest agreement."""

from __future__ import annotations

import hashlib
import json

from hypothesis import example, given
from hypothesis import strategies as st

from osm_polygon_description_tag.runtime.serialization import (
    canonical_json_bytes,
    canonical_json_text,
    sha256_json,
)

_KEYS = st.text(max_size=6)
_JSON = st.recursive(
    st.one_of(
        st.none(),
        st.booleans(),
        st.integers(min_value=-(2**53), max_value=2**53),
        st.floats(allow_nan=False, allow_infinity=False),
        st.text(max_size=8),
    ),
    lambda children: st.one_of(
        st.lists(children, max_size=4), st.dictionaries(_KEYS, children, max_size=4)
    ),
    max_leaves=12,
)


def _reordered(value: object, reverse: bool) -> object:
    if isinstance(value, dict):
        items = [(key, _reordered(item, reverse)) for key, item in value.items()]
        return dict(reversed(items) if reverse else items)
    if isinstance(value, list):
        return [_reordered(item, reverse) for item in value]
    return value


@given(_JSON)
@example({"é": "ü", "a": [1, 2.5, None]})
def test_canonical_text_round_trips_through_json(payload: object) -> None:
    assert json.loads(canonical_json_text(payload)) == payload


@given(_JSON)
def test_key_order_never_changes_the_bytes_or_the_digest(payload: object) -> None:
    shuffled = _reordered(payload, reverse=True)

    assert canonical_json_bytes(shuffled) == canonical_json_bytes(payload)
    assert sha256_json(shuffled) == sha256_json(payload)


@given(_JSON)
def test_bytes_are_the_text_plus_a_newline_and_the_digest_hashes_the_text(payload: object) -> None:
    """Keep the durable LF terminator; digests intentionally hash bare JSON text."""
    text = canonical_json_text(payload)

    assert canonical_json_bytes(payload) == (text + "\n").encode()
    assert sha256_json(payload) == hashlib.sha256(text.encode()).hexdigest()
    assert "\n" not in text
