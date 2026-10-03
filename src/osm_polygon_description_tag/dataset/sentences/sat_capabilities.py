"""Read the pinned shared model facts without network or model dependencies.

Only capability facts are shared. Detector aliases, runtime model revisions,
and published fingerprints belong to the consumer. See docs/sat-capabilities.md.
"""

from __future__ import annotations

import hashlib
import json
from importlib.resources import files

REFERENCE_VERSION = "sat-3l-sm-v1"
# SHA-256 of the complete packaged file, including its embedded payload digest.
REFERENCE_SHA256 = "08fd8190cac25bf35c6d8b99211fffc6d7e93122da47c008366ae80a08107874"


def parse_supported_languages(content: bytes) -> tuple[str, ...]:
    """Fail closed unless the reference bytes match the reviewed local pin."""
    if hashlib.sha256(content).hexdigest() != REFERENCE_SHA256:
        raise ValueError("SaT capability reference digest mismatch")
    return tuple(json.loads(content)["supported_languages"])


def load_supported_languages() -> tuple[str, ...]:
    """Load the wheel's local reference, independently of the working directory."""
    content = (
        files("osm_polygon_description_tag").joinpath("_data/sat-capabilities.json").read_bytes()
    )
    return parse_supported_languages(content)
