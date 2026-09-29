"""Guard the shape of the test suite: no copy-pasted tests, no monolithic files."""

from __future__ import annotations

import ast
from collections import defaultdict
from pathlib import Path

TESTS = Path(__file__).resolve().parents[1]
MAX_LINES = 1000


def _test_files() -> list[Path]:
    return sorted(path for path in TESTS.rglob("*.py") if "__pycache__" not in path.parts)


def test_no_test_function_name_is_defined_twice() -> None:
    homes: dict[str, list[str]] = defaultdict(list)
    for path in _test_files():
        for node in ast.parse(path.read_text(encoding="utf-8")).body:
            if isinstance(node, ast.FunctionDef) and node.name.startswith("test_"):
                homes[node.name].append(path.relative_to(TESTS).as_posix())

    assert {name: files for name, files in homes.items() if len(files) > 1} == {}


def test_no_test_file_exceeds_the_size_limit() -> None:
    sizes = {
        path.relative_to(TESTS).as_posix(): len(path.read_text(encoding="utf-8").splitlines())
        for path in _test_files()
    }

    assert {name: size for name, size in sizes.items() if size > MAX_LINES} == {}


def test_temporary_directories_come_from_pytest_not_mkdtemp() -> None:
    """``mkdtemp`` leaks a directory per run and ignores pytest's cleanup."""
    calls = [
        path.relative_to(TESTS).as_posix()
        for path in _test_files()
        if path != Path(__file__).resolve()
        for node in ast.walk(ast.parse(path.read_text(encoding="utf-8")))
        if isinstance(node, ast.Attribute) and node.attr == "mkdtemp"
    ]

    assert calls == []
