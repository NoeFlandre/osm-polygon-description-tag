"""``pragma: no mutate`` lines in ``src/`` only decrease.

Each pragma line hides every mutant on the statement it marks from the mutation
gate, so the count must not rise. Lower ``MAX_NO_MUTATE_PRAGMA_LINES`` when a
pragma is removed; never raise it.
"""

from __future__ import annotations

import re
import tomllib
from pathlib import Path

# mutmut runs this test from a copy of the repository named ``mutants/``. That copy's
# ``src/`` holds generated mutant code that repeats pragma comments, so counting there
# gives a different total. Count the real repository's source in that case.
_TEST_TREE_ROOT = Path(__file__).resolve().parents[2]
PROJECT_ROOT = _TEST_TREE_ROOT.parent if _TEST_TREE_ROOT.name == "mutants" else _TEST_TREE_ROOT
PRAGMA = "pragma: no mutate"
MAX_NO_MUTATE_PRAGMA_LINES = 110


def _pragma_lines() -> list[str]:
    hits: list[str] = []
    for path in sorted((PROJECT_ROOT / "src").rglob("*.py")):
        for line_number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
            if PRAGMA in line:
                hits.append(f"{path.relative_to(PROJECT_ROOT)}:{line_number}: {line.strip()}")
    return hits


def test_no_mutate_pragma_lines_only_decrease() -> None:
    hits = _pragma_lines()

    assert len(hits) <= MAX_NO_MUTATE_PRAGMA_LINES, "\n".join(hits)


def test_cast_calls_are_excluded_by_mutmut_config_not_by_pragmas() -> None:
    project = tomllib.loads((PROJECT_ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    patterns = project["tool"]["mutmut"]["do_not_mutate_patterns"]

    assert any(re.search(pattern, "value = cast(int, row['x'])") for pattern in patterns)
    assert not any(re.search(pattern, "value = broadcast(int, row)") for pattern in patterns)
    assert [hit for hit in _pragma_lines() if "cast(" in hit] == []
