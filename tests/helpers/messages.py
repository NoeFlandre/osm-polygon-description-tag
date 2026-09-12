"""Anchor a refusal message so a corrupted one cannot still satisfy the test.

``pytest.raises(match=...)`` searches, so a substring keeps passing when the
message it came from gains, loses, or changes surrounding text. Every refusal
in this project is operator-facing --- it is what someone reads when a shard
stalls on Grid'5000 --- so the whole message is part of the contract.
"""

from __future__ import annotations

import re

__all__ = ["exactly"]


def exactly(message: str) -> str:
    """Return a pattern that matches ``message`` and nothing else."""
    return rf"\A{re.escape(message)}\Z"
