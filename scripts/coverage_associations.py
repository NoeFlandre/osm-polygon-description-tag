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

    spans: dict[str, tuple[int, int]] = {}

    def visit(node: ast.AST, class_name: str | None) -> None:
        for child in ast.iter_child_nodes(node):
            if isinstance(child, ast.ClassDef):
                visit(child, child.name)
                continue
            if isinstance(child, ast.FunctionDef | ast.AsyncFunctionDef):
                mangled = (
                    f"{module}.xǁ{class_name}ǁ{child.name}"
                    if class_name is not None
                    else f"{module}.x_{child.name}"
                )
                end = child.end_lineno or child.lineno
                spans[mangled] = (child.lineno, end)
                visit(child, class_name)
                continue
            visit(child, class_name)

    visit(ast.parse(source), None)
    return spans


def associations_for_file(
    path: Path,
    module: str,
    contexts_by_line: Mapping[int, Iterable[str]],
) -> dict[str, tuple[str, ...]]:
    """Return the covering tests of every function defined in one file."""

    spans = mangled_names(path.read_text(encoding="utf-8"), module)
    covering: dict[str, set[str]] = {name: set() for name in spans}
    for line, contexts in contexts_by_line.items():
        tests = {_strip_phase(item) for item in contexts if item}
        if not tests:
            continue
        for name, (start, end) in spans.items():
            if start <= line <= end:
                covering[name] |= tests
    return {name: tuple(sorted(tests)) for name, tests in covering.items()}


def build_associations(coverage_file: Path, source_root: Path) -> dict[str, tuple[str, ...]]:
    """Return ``{mangled function name: covering tests}`` for the whole package."""

    from coverage import CoverageData

    data = CoverageData(basename=str(coverage_file))
    data.read()
    measured = {Path(name) for name in data.measured_files()}
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
