#!/usr/bin/env python3
"""Verify an installed wheel against the package source tree.

Run with the Python of a clean venv that has only the built wheel installed.
It imports every module (catching missing modules or runtime dependencies)
and checks that every non-Python file in the source package is installed
with identical bytes (catching dropped include globs).
"""

from __future__ import annotations

import argparse
import importlib
import importlib.resources
import pkgutil
import sys
from collections.abc import Iterator, Sequence
from pathlib import Path

PACKAGE = "osm_polygon_description_tag"


def source_data_files(source_root: Path) -> list[str]:
    """Relative paths of every packaged non-Python file under ``source_root``."""
    return sorted(
        path.relative_to(source_root).as_posix()
        for path in source_root.rglob("*")
        if path.is_file() and path.suffix not in {".py", ".pyc"} and "__pycache__" not in path.parts
    )


def data_file_problems(source_root: Path, package: str = PACKAGE) -> Iterator[str]:
    """Yield one message per data file that is missing or differs when installed."""
    installed = importlib.resources.files(package)
    for relative in source_data_files(source_root):
        target = installed.joinpath(*relative.split("/"))
        if not target.is_file():
            yield f"missing from the installed package: {relative}"
        elif target.read_bytes() != (source_root / relative).read_bytes():
            yield f"differs from the source tree: {relative}"


def import_problems(package: str = PACKAGE) -> Iterator[str]:
    """Yield one message per module of ``package`` that fails to import."""
    root = importlib.import_module(package)
    for module in pkgutil.walk_packages(root.__path__, prefix=f"{package}."):
        try:
            importlib.import_module(module.name)
        except Exception as error:
            yield f"cannot import {module.name}: {type(error).__name__}: {error}"


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source-root", type=Path, default=Path("src") / PACKAGE)
    args = parser.parse_args(argv)
    source_root = args.source_root.resolve()
    problems = [*import_problems(), *data_file_problems(source_root)]
    for problem in problems:
        print(problem, file=sys.stderr)
    if problems:
        return 1
    count = len(source_data_files(source_root))
    print(f"installed package OK: all modules import, {count} data files match")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
