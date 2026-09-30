"""Build exact per-function test associations from per-test coverage contexts.

Mutmut associates a function with the tests that entered its trampoline, but
that record is demonstrably incomplete: a helper reached through an adapter or
a patched boundary can end up with no recorded test at all, which makes the
gate either report killable mutants as survivors or fall back to running the
whole suite for every one of that function's mutants.

Coverage contexts give the same association exactly and cheaply. Mutmut mutates
function bodies, so a test that never executes a line of a function cannot
observe that function's mutation: the tests that cover a function are precisely
the tests that can kill its mutants. One suite run under
``--cov-context=test`` therefore yields a sound, minimal selection.
"""

from __future__ import annotations

import ast
from collections.abc import Iterable, Mapping
from pathlib import Path
from typing import Any

_PHASE_SUFFIXES = ("|run", "|setup", "|teardown")


def _strip_phase(context: str) -> str:
    """Return the test node id without pytest-cov's phase suffix."""
    for suffix in _PHASE_SUFFIXES:
        if context.endswith(suffix):
            return context[: -len(suffix)]
    return context


def module_name_for(path: Path, source_root: Path) -> str:
    """Return the dotted module name of one source file.

    Raises ``ValueError`` when the file lies outside ``source_root``, which is
    how coverage entries measured elsewhere get skipped.
    """
    relative = path.resolve().relative_to(source_root.resolve()).with_suffix("")
    parts = list(relative.parts)
    if parts and parts[-1] == "__init__":
        parts.pop()
    return ".".join(parts)


def mangled_names(source: str, module: str) -> dict[str, tuple[int, int]]:
    """Map each mutmut-mangled function name to its inclusive line span.

    Module-level functions mangle to ``module.x_name`` and methods to
    ``module.xǁClassǁname``, which is how mutmut names them in its metadata.
    """

    visitor = _FunctionSpanVisitor(module)
    visitor.visit(ast.parse(source))
    return visitor.spans


class _FunctionSpanVisitor:
    """Walk class and function scopes using the names mutmut records."""

    def __init__(self, module: str) -> None:
        self.module = module
        self.class_name: str | None = None
        self.spans: dict[str, tuple[int, int]] = {}

    def visit(self, node: ast.AST) -> None:
        if isinstance(node, ast.ClassDef):
            self._visit_class(node)
        elif isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef):
            self._record_function(node)
            self._visit_children(node)
        else:
            self._visit_children(node)

    def _visit_class(self, node: ast.ClassDef) -> None:
        previous = self.class_name
        self.class_name = node.name
        self._visit_children(node)
        self.class_name = previous

    def _record_function(self, node: ast.FunctionDef | ast.AsyncFunctionDef) -> None:
        if self.class_name is None:
            mangled = f"{self.module}.x_{node.name}"
        else:
            mangled = f"{self.module}.xǁ{self.class_name}ǁ{node.name}"
        self.spans[mangled] = (node.body[0].lineno, node.end_lineno or node.lineno)

    def _visit_children(self, node: ast.AST) -> None:
        for child in ast.iter_child_nodes(node):
            self.visit(child)


def associations_for_file(
    path: Path,
    module: str,
    contexts_by_line: Mapping[int, Iterable[str]],
) -> dict[str, tuple[str, ...]]:
    """Return the covering tests of every function defined in one file."""

    spans = mangled_names(path.read_text(encoding="utf-8"), module)
    contexts = _test_contexts_by_line(contexts_by_line)
    return {name: _tests_for_span(start, end, contexts) for name, (start, end) in spans.items()}


def _test_contexts_by_line(
    contexts_by_line: Mapping[int, Iterable[str]],
) -> dict[int, tuple[str, ...]]:
    return {
        line: tuple(sorted({_strip_phase(context) for context in contexts if context}))
        for line, contexts in contexts_by_line.items()
    }


def _tests_for_span(
    start: int, end: int, contexts_by_line: Mapping[int, tuple[str, ...]]
) -> tuple[str, ...]:
    tests: set[str] = set()
    for line, contexts in contexts_by_line.items():
        if start <= line <= end:
            tests.update(contexts)
    return tuple(sorted(tests))


def build_associations(coverage_file: Path, source_root: Path) -> dict[str, tuple[str, ...]]:
    """Return ``{mangled function name: covering tests}`` for the whole package."""

    from coverage import CoverageData

    data = CoverageData(basename=str(coverage_file))
    data.read()
    return _associations_for_files(data.measured_files(), data, source_root)


def _associations_for_files(
    measured_files: Iterable[str], data: Any, source_root: Path
) -> dict[str, tuple[str, ...]]:
    measured = {Path(name) for name in measured_files}
    associations: dict[str, tuple[str, ...]] = {}
    for path in sorted(measured):
        if path.suffix != ".py":
            continue
        try:
            module = module_name_for(path, source_root)
        except ValueError:
            continue
        associations.update(associations_for_file(path, module, data.contexts_by_lineno(str(path))))
    return associations


__all__ = [
    "associations_for_file",
    "build_associations",
    "mangled_names",
    "module_name_for",
]
