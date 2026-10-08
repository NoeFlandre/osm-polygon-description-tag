"""The mutmut exclusion for ``cast(`` matches bare calls only.

``typing.cast`` returns its value unchanged, so mutating its type argument is
equivalent and the pattern skips the line. A method call such as pyarrow's
``table.cast(schema)`` changes the table, so its value argument is a real mutant
and must not be skipped.
"""

from __future__ import annotations

import re
import tomllib
from pathlib import Path

import pytest

PROJECT_ROOT = Path(__file__).resolve().parents[2]


def _mutmut_patterns() -> list[str]:
    project = tomllib.loads((PROJECT_ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    return project["tool"]["mutmut"]["do_not_mutate_patterns"]


def _skipped_by_mutmut(line: str) -> bool:
    return any(re.search(pattern, line) for pattern in _mutmut_patterns())


@pytest.mark.parametrize(
    "line",
    [
        "v = cast(int, r)",
        "    return cast(list[str], value)",
        "value = float(cast(float, record['x']))",
    ],
)
def test_mutmut_skips_bare_cast_calls(line: str) -> None:
    assert _skipped_by_mutmut(line)


@pytest.mark.parametrize(
    "line",
    [
        "w = t.cast(s)",
        "x = batch.column(n).cast(t)",
        "writer.write_table(repaired.cast(schema))",
        "value = broadcast(int, row)",
    ],
)
def test_mutmut_keeps_method_cast_calls_under_mutation(line: str) -> None:
    assert not _skipped_by_mutmut(line)
