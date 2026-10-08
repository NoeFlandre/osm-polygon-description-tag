"""Reproducer: CRLF bytes through the two read_text write paths (temp inputs only).

Usage (from the repo root, with the repo's virtualenv):
    REPO_SRC=/path/to/osm-polygon-description-tag/src python -I crlf/repro_crlf.py
"""

from __future__ import annotations

import os
import sys
import tempfile
from pathlib import Path

SRC = os.environ.get("REPO_SRC")
if not SRC:
    raise SystemExit("set REPO_SRC to the repo's src directory")
if SRC not in sys.path:
    sys.path.insert(0, SRC)

from osm_polygon_description_tag.dataset import docs as docs_module  # noqa: E402
from osm_polygon_description_tag.dataset.geography import card as card_module  # noqa: E402

LF_CARD = (
    "---\ntitle: café\n---\n\nIntro text.\n\n"
    "<!-- GENERATED:STATS:START -->\nstats\n<!-- GENERATED:STATS:END -->\n"
)


def crlf_bytes() -> bytes:
    return LF_CARD.replace("\n", "\r\n").encode("utf-8")


def counts(data: bytes) -> str:
    return f"CRLF={data.count(b'\r\n')} bare_LF={data.count(b'\n') - data.count(b'\r\n')} CR={data.count(b'\r')}"


# Path A: dataset/geography/card.py write_map_block_marker_to_template
work_a = Path(tempfile.mkdtemp(prefix="card-"))
template_a = work_a / "card.md"
before_a = crlf_bytes()
template_a.write_bytes(before_a)
print("A before bytes:", before_a)
print("A before counts:", counts(before_a))
card_module.write_map_block_marker_to_template(template_a)
after_a = template_a.read_bytes()
print("A after bytes:", after_a)
print("A after counts:", counts(after_a))
print("A CR survives:", b"\r" in after_a)

# Path B: dataset/docs.py _card_source, template branch (preserve_existing=False)
work_b = Path(tempfile.mkdtemp(prefix="docs-"))
template_b = work_b / "dataset-card-template.md"
before_b = crlf_bytes()
template_b.write_bytes(before_b)
source_b, published_b = docs_module._card_source(work_b, template_b, preserve_existing=False)
print("B template before counts:", counts(before_b))
print("B _card_source returns CR:", "\r" in source_b, "| published:", published_b)
print("B encoded for write has CR:", b"\r" in source_b.encode("utf-8"))

# Path C: dataset/docs.py _card_source, published README branch (preserve_existing=True)
(work_b / "README.md").write_bytes(before_b)
source_c, published_c = docs_module._card_source(work_b, template_b, preserve_existing=True)
print("C README before counts:", counts(before_b))
print("C _card_source returns CR:", "\r" in source_c, "| published:", published_c)
print("C encoded for write has CR:", b"\r" in source_c.encode("utf-8"))
