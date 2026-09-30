"""Print the source diff of named mutants without running the mutation gate.

Reading a survivor's diff used to mean waiting for the sharded gate -- tens of
minutes in CI, longer locally -- because the mutated sources are a by-product of
a full run. mutmut can produce them directly: ``mutate_file_contents`` is a pure
function of one file's text, so a survivor's diff is a couple of seconds of
parsing rather than a full test campaign.

    python scripts/show_mutant.py \
        osm_polygon_description_tag.dataset.stats.x_collect_stats__mutmut_24

Mutants are named as the gate reports them, so a name can be pasted straight
from a shard's log. Several may be given at once, and they are grouped by file
so each file is parsed only once.

The ids are positions in the list of mutations mutmut found for that function,
so editing the file renumbers them. A name is only meaningful against the
revision that reported it; re-read the survivors after changing the source
rather than carrying ids across a commit.
"""

from __future__ import annotations

import argparse
import ast
import difflib
import sys
from collections import defaultdict
from pathlib import Path
from typing import Any

MUTANT_MARKER = "__mutmut_"
SOURCE_ROOT = Path("src")


def split_mutant_name(name: str) -> tuple[str, str, str]:
    """Split a reported mutant into its module, function and mutant id.

    ``a.b.c.x_func__mutmut_3`` is the function ``func`` of module ``a.b.c``,
    mutated in the third way mutmut found for it. Class methods are reported as
    ``xǁClassǁmethod``, which stays in the function part untouched.
    """
    if MUTANT_MARKER not in name:
        raise ValueError(f"not a mutant name: {name}")
    qualified, _, mutant_id = name.rpartition(MUTANT_MARKER)
    module_name, _, function_part = qualified.rpartition(".")
    if not module_name or not function_part:
        raise ValueError(f"not a mutant name: {name}")
    return module_name, function_part, mutant_id


def module_path(module_name: str, *, source_root: Path = SOURCE_ROOT) -> Path:
    """Return the file defining a dotted module, package ``__init__`` included."""
    relative = Path(*module_name.split("."))
    candidates = (source_root / relative.with_suffix(".py"), source_root / relative / "__init__.py")
    for candidate in candidates:
        if candidate.is_file():
            return candidate
    raise FileNotFoundError(f"no source file for module {module_name}")


def _functions_in(source: str) -> dict[str, str]:
    """Return every function in ``source`` by name, including nested classes."""
    functions: dict[str, str] = {}

    def walk(node: ast.AST) -> None:
        for child in getattr(node, "body", []):
            if isinstance(child, ast.FunctionDef | ast.AsyncFunctionDef):
                segment = ast.get_source_segment(source, child)
                if segment is not None:
                    functions[child.name] = segment
            if isinstance(child, ast.ClassDef):
                walk(child)

    walk(ast.parse(source))
    return functions


def mutant_diff(mutated_source: str, function_part: str, mutant_id: str) -> str:
    """Return the unified diff between a function's original and mutated body."""
    functions = _functions_in(mutated_source)
    original, mutated = _mutant_functions(functions, function_part, mutant_id)
    changed = _changed_function_lines(original, mutated)
    return "\n".join(changed) if changed else "(no textual difference: equivalent mutant)"


def _mutant_functions(
    functions: dict[str, str], function_part: str, mutant_id: str
) -> tuple[str, str]:
    original = functions.get(f"{function_part}{MUTANT_MARKER}orig")
    mutated = functions.get(f"{function_part}{MUTANT_MARKER}{mutant_id}")
    if original is None:
        raise KeyError(f"no mutants generated for {function_part}")
    if mutated is None:
        raise KeyError(f"no mutant {mutant_id} for {function_part}")
    return original, mutated


def _changed_function_lines(original: str, mutated: str) -> list[str]:
    diff = difflib.unified_diff(original.splitlines(), mutated.splitlines(), lineterm="", n=0)
    # The renamed def line differs for every mutant and says nothing.
    return [line for line in diff if _is_mutant_change(line)]


def _is_mutant_change(line: str) -> bool:
    if not line.startswith(("+", "-")) or line.startswith(("+++", "---")):
        return False
    return MUTANT_MARKER not in line


def _mutants_by_module(names: list[str]) -> dict[str, list[tuple[str, str, str]]]:
    by_module: dict[str, list[tuple[str, str, str]]] = defaultdict(list)
    for name in names:
        module_name, function_part, mutant_id = split_mutant_name(name)
        by_module[module_name].append((name, function_part, mutant_id))
    return by_module


def _print_module_mutants(
    module_name: str,
    entries: list[tuple[str, str, str]],
    *,
    source_root: Path,
    mutate_file_contents: Any,
) -> bool:
    path = module_path(module_name, source_root=source_root)
    source = path.read_text(encoding="utf-8")
    mutated_source = mutate_file_contents(str(path), source).code
    failed = False
    for name, function_part, mutant_id in entries:
        print(f"=== {name}\n--- {path}")
        try:
            print(mutant_diff(mutated_source, function_part, mutant_id))
        except KeyError as error:
            failed = True
            print(f"!!! {error}", file=sys.stderr)
    return failed


def _print_mutants(names: list[str], source_root: Path) -> bool:
    # Imported here so --help works without mutmut installed.
    from mutmut.__main__ import mutate_file_contents

    failed = False
    for module_name, entries in _mutants_by_module(names).items():
        failed = (
            _print_module_mutants(
                module_name,
                entries,
                source_root=source_root,
                mutate_file_contents=mutate_file_contents,
            )
            or failed
        )
    return failed


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("mutants", nargs="+", help="mutant names as the gate reports them")
    parser.add_argument(
        "--source-root",
        type=Path,
        default=SOURCE_ROOT,
        help="directory holding the package sources (default: src)",
    )
    args = parser.parse_args()
    if _print_mutants(args.mutants, args.source_root):
        raise SystemExit(1)


if __name__ == "__main__":
    main()
