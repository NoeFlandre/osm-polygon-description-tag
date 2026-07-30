"""Static contracts for the canonical package dependency direction."""

from __future__ import annotations

import ast
from pathlib import Path

import pytest

import osm_polygon_description_tag

PACKAGE_ROOT = Path(osm_polygon_description_tag.__file__).parent
CANONICAL_DEPENDENCIES = {
    "runtime": {"runtime"},
    "osm": {"runtime", "osm"},
    "dataset": {"runtime", "osm", "dataset"},
    "publication": {"runtime", "dataset", "publication"},
    "workflow": {"runtime", "osm", "dataset", "publication", "workflow"},
}
COMPATIBILITY_MODULES = (
    "_logging",
    "_resources",
    "config",
    "discovery",
    "extraction",
    "manifest",
    "orchestrator",
    "pipeline",
    "reporting",
    "schema",
    "storage",
    "transform",
)


def _package_imports(path: Path) -> list[str]:
    imports: list[str] = []
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    module_parts = list(path.relative_to(PACKAGE_ROOT).with_suffix("").parts)
    package_parts = module_parts[:-1]
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imports.extend(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom):
            if node.level:
                base = package_parts[: len(package_parts) - node.level + 1]
                imported = [*base, *(node.module or "").split(".")]
                imports.append(
                    ".".join(["osm_polygon_description_tag", *(part for part in imported if part)])
                )
            elif node.module is not None:
                imports.append(node.module)
    return [
        name.removeprefix("osm_polygon_description_tag.")
        for name in imports
        if name.startswith("osm_polygon_description_tag.")
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


def test_cli_imports_only_canonical_packages() -> None:
    canonical_packages = set(CANONICAL_DEPENDENCIES)
    imports = _package_imports(PACKAGE_ROOT / "cli.py")
    violations = [
        imported_module
        for imported_module in imports
        if imported_module.split(".", maxsplit=1)[0] not in canonical_packages
    ]
    assert violations == []


@pytest.mark.parametrize("module_name", COMPATIBILITY_MODULES)
def test_compatibility_module_is_a_pure_import_shim(module_name: str) -> None:
    path = PACKAGE_ROOT / f"{module_name}.py"
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    statements = list(tree.body)
    if statements and isinstance(statements[0], ast.Expr):
        assert isinstance(statements[0].value, ast.Constant)
        assert isinstance(statements[0].value.value, str)
        statements.pop(0)

    assignments = [node for node in statements if isinstance(node, ast.Assign)]
    assert len(assignments) == 1
    assignment = assignments[0]
    assert len(assignment.targets) == 1
    assert isinstance(assignment.targets[0], ast.Name)
    assert assignment.targets[0].id == "__all__"
    assert isinstance(assignment.value, ast.List | ast.Tuple)
    assert all(
        isinstance(element, ast.Constant) and isinstance(element.value, str)
        for element in assignment.value.elts
    )

    disallowed = [
        type(node).__name__
        for node in statements
        if not isinstance(node, ast.Import | ast.ImportFrom | ast.Assign)
    ]
    assert disallowed == []
