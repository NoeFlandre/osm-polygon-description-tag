"""Quality report serialization is deterministic and creates its destination."""

from __future__ import annotations

import json
from pathlib import Path

from scripts.report_io import json_document, write_json_report, write_text_report


def test_json_document_keeps_unicode_newline_and_sorted_keys() -> None:
    assert (
        json_document({"z": 1, "é": "value", "a": 2})
        == '{\n  "a": 2,\n  "z": 1,\n  "é": "value"\n}\n'
    )


def test_json_document_supports_compact_mutation_state() -> None:
    assert (
        json_document({"b": [2], "a": 1}, indent=None, separators=(",", ":")) == '{"a":1,"b":[2]}\n'
    )


def test_report_writers_create_parent_directories(tmp_path: Path) -> None:
    json_path = tmp_path / "nested" / "report.json"
    text_path = tmp_path / "other" / "report.md"

    write_json_report(json_path, {"ok": True})
    write_text_report(text_path, "report\n")

    assert json.loads(json_path.read_text(encoding="utf-8")) == {"ok": True}
    assert text_path.read_text(encoding="utf-8") == "report\n"
