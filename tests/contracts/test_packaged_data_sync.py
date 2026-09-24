"""Every packaged data file must stay byte-identical to its maintained copies.

The files under ``src/osm_polygon_description_tag/_data/`` are committed, not
generated at build time, so nothing but these tests keeps them in sync with the
copies the repository also maintains elsewhere.
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
PACKAGED = REPO_ROOT / "src" / "osm_polygon_description_tag" / "_data"

MIRRORS = [
    ("osmium-export.json", Path("config/osmium-export.json")),
    ("dataset-card-hero.png", Path("assets/dataset-card-hero.png")),
    ("dataset-card-hero.png", Path("slides/assets/dataset-card-hero.png")),
    ("dataset-card-template.md", Path("docs/dataset-card-template.md")),
]


@pytest.mark.parametrize(("packaged_name", "mirror"), MIRRORS, ids=lambda value: str(value))
def test_packaged_file_is_byte_identical_to_its_mirror(packaged_name: str, mirror: Path) -> None:
    if "MUTANT_UNDER_TEST" in os.environ:
        pytest.skip("repository copies are checked on the canonical source tree")
    assert (PACKAGED / packaged_name).read_bytes() == (REPO_ROOT / mirror).read_bytes()
