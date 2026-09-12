"""One canonical JSON encoding for everything this project fingerprints.

Checkpoints, receipts, model identities and the splitter's supported-language
set are all bound by a digest over JSON, and a digest is only comparable while
every producer encodes the same way. The encoding lives here, in the lowest
layer, so every caller shares it rather than restating it and drifting.
"""

from __future__ import annotations

import json

__all__ = ["canonical_json_bytes"]


def canonical_json_bytes(payload: object) -> bytes:
    """Encode ``payload`` as deterministic UTF-8 JSON with a trailing newline."""
    # pragma: no mutate start - ensure_ascii=None equals False; exact bytes are tested
    text = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    # pragma: no mutate end
    return (text + "\n").encode()
