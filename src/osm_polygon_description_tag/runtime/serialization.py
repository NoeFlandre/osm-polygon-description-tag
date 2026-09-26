"""One canonical JSON encoding for everything this project fingerprints.

Checkpoints, receipts, model identities and the splitter's supported-language
set are all bound by a digest over JSON, and a digest is only comparable while
every producer encodes the same way. The encoding lives here, in the lowest
layer, so every caller shares it rather than restating it and drifting.
"""

from __future__ import annotations

import hashlib
import json

__all__ = ["canonical_json_bytes", "canonical_json_text", "sha256_json"]


def canonical_json_text(payload: object) -> str:
    """Encode ``payload`` as deterministic JSON text without a trailing newline."""
    # pragma: no mutate start - ensure_ascii=None equals False; exact bytes are tested
    return json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    # pragma: no mutate end


def canonical_json_bytes(payload: object) -> bytes:
    """Encode ``payload`` as deterministic UTF-8 JSON with a trailing newline."""
    return (canonical_json_text(payload) + "\n").encode()


def sha256_json(payload: object) -> str:
    """Return the hex SHA-256 of ``payload``'s canonical text (no trailing newline)."""
    return hashlib.sha256(canonical_json_text(payload).encode()).hexdigest()
