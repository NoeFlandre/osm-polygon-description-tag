"""Static contracts for the canonical package dependency direction."""

from __future__ import annotations

import ast
import os
from pathlib import Path

import pytest

import osm_polygon_description_tag

PACKAGE_ROOT = Path(osm_polygon_description_tag.__file__).parent
CANONICAL_DEPENDENCIES = {
    "runtime": {"runtime"},
    "osm": {"runtime", "osm"},
    "dataset": {"runtime", "osm", "dataset"},
    "publication": {"runtime", "dataset", "publication"},
    "observability": {"runtime", "dataset", "observability"},
    "workflow": {"runtime", "osm", "dataset", "publication", "observability", "workflow"},
}
CONSOLE_MODULES = ("cli", "language_cli")
CONSOLE_SUPPORT_MODULES = {
    "grid_transport",
    "grid_workflow",
    "language_workflow",
    "publication_workflow",
}


def _package_imports_from_source(source: str, module_parts: list[str]) -> list[str]:
    imports: list[str] = []
    tree = ast.parse(source)
    package_parts = module_parts[:-1]
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imports.extend(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom):
            if node.level:
                base = package_parts[: len(package_parts) - node.level + 1]
                imported = [
                    "osm_polygon_description_tag",
                    *base,
                    *(node.module or "").split("."),
                ]
            elif node.module is not None:
                imported = node.module.split(".")
            else:
                continue
            imported = [part for part in imported if part]
            if imported == ["osm_polygon_description_tag"] or node.module is None:
                imports.extend(".".join([*imported, alias.name]) for alias in node.names)
            else:
                imports.append(".".join(imported))
    return [
        name.removeprefix("osm_polygon_description_tag.")
        for name in imports
        if name.startswith("osm_polygon_description_tag.")
    ]


def _package_imports(path: Path) -> list[str]:
    module_parts = list(path.relative_to(PACKAGE_ROOT).with_suffix("").parts)
    return _package_imports_from_source(path.read_text(encoding="utf-8"), module_parts)


def _forbidden_imports(source: str, module_parts: list[str], allowed_layers: set[str]) -> list[str]:
    return [
        imported_module
        for imported_module in _package_imports_from_source(source, module_parts)
        if imported_module.split(".", maxsplit=1)[0] not in allowed_layers
    ]


@pytest.mark.parametrize("package_name", CANONICAL_DEPENDENCIES)
def test_canonical_package_imports_only_allowed_lower_layers(package_name: str) -> None:
    allowed = CANONICAL_DEPENDENCIES[package_name]
    violations: list[str] = []
    for path in sorted((PACKAGE_ROOT / package_name).glob("*.py")):
        for imported_module in _package_imports(path):
            imported_layer = imported_module.split(".", maxsplit=1)[0]
            if imported_layer not in allowed:
                violations.append(f"{path.relative_to(PACKAGE_ROOT)} imports {imported_module}")
    assert violations == []


@pytest.mark.parametrize("module_name", CONSOLE_MODULES)
def test_console_modules_import_only_canonical_packages(module_name: str) -> None:
    """Console entry points compose canonical packages and never the shims."""
    allowed = set(CANONICAL_DEPENDENCIES) | set(CONSOLE_MODULES) | CONSOLE_SUPPORT_MODULES
    imports = _package_imports(PACKAGE_ROOT / f"{module_name}.py")
    violations = [
        imported_module
        for imported_module in imports
        if imported_module.split(".", maxsplit=1)[0] not in allowed
    ]
    assert violations == []


def test_grid_operator_is_split_into_cohesive_package_modules() -> None:
    if "MUTANT_UNDER_TEST" in os.environ:
        pytest.skip("static architecture bounds are checked on the canonical source tree")
    package_dir = PACKAGE_ROOT / "workflow" / "grid_operator"
    assert package_dir.is_dir()
    assert not (PACKAGE_ROOT / "workflow" / "grid_operator.py").exists()
    modules = sorted(package_dir.glob("*.py"))
    assert {path.name for path in modules} >= {
        "__init__.py",
        "bundle.py",
        "models.py",
        "script.py",
        "submission.py",
        "verify.py",
    }
    assert all(len(path.read_text(encoding="utf-8").splitlines()) <= 500 for path in modules)


@pytest.mark.parametrize(
    ("source", "expected"),
    [
        ("from osm_polygon_description_tag import manifest", ["manifest"]),
        ("from .. import manifest", ["manifest"]),
        ("from . import cleanup", ["runtime.cleanup"]),
    ],
)
def test_import_parser_resolves_package_root_and_relative_aliases(
    source: str, expected: list[str]
) -> None:
    assert _package_imports_from_source(source, ["runtime", "example"]) == expected


@pytest.mark.parametrize(
    "source",
    [
        "from osm_polygon_description_tag import manifest",
        "from .. import manifest",
    ],
)
def test_alias_imports_cannot_evade_lower_layer_contract(source: str) -> None:
    assert _forbidden_imports(source, ["runtime", "example"], {"runtime"}) == ["manifest"]
