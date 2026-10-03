"""Report SaT reference drift across Description, Website, and Wikidata.

Use checkout paths for an offline comparison. Omit a path to read that public
repository at its requested ref. This audit is never imported by runtime code.
"""

from __future__ import annotations

import argparse
import hashlib
import json
from collections.abc import Sequence
from pathlib import Path
from urllib.parse import quote
from urllib.request import urlopen

REFERENCES = {
    "description": (
        "osm-polygon-description-tag",
        "src/osm_polygon_description_tag/_data/sat-capabilities.json",
    ),
    "website": (
        "osm-polygon-website-tag",
        "src/osm_polygon_website_tag/pipeline/sat-capabilities.json",
    ),
    "wikidata": (
        "osm-polygon-wikidata-only",
        "src/osm_polygon_wikidata_only/v2/sat-capabilities.json",
    ),
}


def reference_identity(content: bytes) -> tuple[str, str]:
    """Validate the payload digest and return its version and complete-file pin."""
    reference = json.loads(content)
    digest = reference.pop("digest")
    canonical = json.dumps(reference, sort_keys=True, separators=(",", ":")).encode()
    if digest != "sha256:" + hashlib.sha256(canonical).hexdigest():
        raise ValueError("invalid capability payload digest")
    return reference["reference_version"], hashlib.sha256(content).hexdigest()


def read_reference(name: str, checkout: Path | None, ref: str) -> bytes:
    """Read a local checkout or a fixed public GitHub repository, with a timeout."""
    repository, path = REFERENCES[name]
    if checkout is not None:
        return (checkout / path).read_bytes()
    url = f"https://raw.githubusercontent.com/NoeFlandre/{repository}/{quote(ref, safe='')}/{path}"
    with urlopen(url, timeout=30) as response:  # noqa: S310 - fixed HTTPS origin and repositories
        return response.read(65536)


def compare_references(references: dict[str, bytes]) -> bool:
    """Print every version and digest; different bytes mean consumers have drifted."""
    identities = {name: reference_identity(content) for name, content in references.items()}
    for name, (version, digest) in identities.items():
        print(f"{name}: {version} sha256:{digest}")
    return len(set(identities.values())) == 1


def main(argv: Sequence[str] | None = None) -> int:
    """Return nonzero for missing, corrupt, or divergent references."""
    parser = argparse.ArgumentParser(description=__doc__)
    for name in REFERENCES:
        parser.add_argument(f"--{name}", type=Path, help="local checkout (otherwise fetch GitHub)")
        parser.add_argument(f"--{name}-ref", default="main", help="Git ref for a remote comparison")
    args = parser.parse_args(argv)
    references = {
        name: read_reference(name, getattr(args, name), getattr(args, f"{name}_ref"))
        for name in REFERENCES
    }
    if not compare_references(references):
        print("DRIFT: use reviewed PRs to align capability references; never update at runtime.")
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
