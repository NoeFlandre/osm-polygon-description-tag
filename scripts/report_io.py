"""Stable persistence for machine-readable and text quality reports."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any


def json_document(
    payload: Any,
    *,
    indent: int | None = 2,
    sort_keys: bool = True,
    separators: tuple[str, str] | None = None,
) -> str:
    """Serialize one report as deterministic UTF-8 text terminated by a newline."""
    return (
        json.dumps(
            payload,
            ensure_ascii=False,
            indent=indent,
            sort_keys=sort_keys,
            separators=separators,
        )
        + "\n"
    )


def write_json_report(
    path: Path,
    payload: Any,
    *,
    indent: int | None = 2,
    sort_keys: bool = True,
    separators: tuple[str, str] | None = None,
) -> None:
    """Create a report directory and write the requested stable JSON format."""
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json_document(payload, indent=indent, sort_keys=sort_keys, separators=separators),
        encoding="utf-8",
    )


def write_text_report(path: Path, content: str) -> None:
    """Create a report directory and write a UTF-8 text report."""
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")
