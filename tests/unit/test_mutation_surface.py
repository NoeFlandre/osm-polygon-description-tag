"""Direct contracts for small public-boundary helpers.

These tests deliberately exercise helpers that are otherwise reached only by
CLI error paths or optional integrations.  Keeping those boundaries explicit
gives mutation testing a meaningful oracle without touching real data or the
Hub.
"""

from __future__ import annotations

import hashlib

from osm_polygon_description_tag.dataset.manifest import _empty_policy_hash
from osm_polygon_description_tag.dataset.text import trimmed_nonempty_text


def test_shared_text_contract_is_on_the_mutation_surface() -> None:
    assert trimmed_nonempty_text("  description  ") == "description"


def test_empty_policy_hash_is_sha256_of_empty_bytes() -> None:
    assert _empty_policy_hash() == hashlib.sha256(b"").hexdigest()
