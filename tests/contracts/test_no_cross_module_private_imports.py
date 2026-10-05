"""Leading underscores mean module-private: no cross-module imports of such names."""

import ast
from pathlib import Path

import osm_polygon_description_tag

_SOURCE_ROOT = Path(osm_polygon_description_tag.__file__).parent


def _private_imports(path: Path) -> list[str]:
    tree = ast.parse(path.read_text(encoding="utf-8"))
    return [
        f"{path.relative_to(_SOURCE_ROOT)}:{node.lineno} {alias.name}"
        for node in ast.walk(tree)
        if isinstance(node, ast.ImportFrom) and (node.level or node.module)
        for alias in node.names
        if alias.name.startswith("_") and not alias.name.startswith("__")
    ]


def test_source_never_imports_underscore_names_from_other_modules() -> None:
    offenders = [
        line for path in sorted(_SOURCE_ROOT.rglob("*.py")) for line in _private_imports(path)
    ]
    assert offenders == []


def test_module_class_shims_are_gone() -> None:
    offenders = [
        str(path.relative_to(_SOURCE_ROOT))
        for path in _SOURCE_ROOT.rglob("*.py")
        if "__class__ =" in path.read_text(encoding="utf-8")
    ]
    assert offenders == []
