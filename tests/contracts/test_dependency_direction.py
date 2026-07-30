"""Static contracts for the canonical package dependency direction."""

from __future__ import annotations

import ast
import re
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
SHIM_TARGETS = {
    "_logging": {"osm_polygon_description_tag.runtime.logging"},
    "_resources": {"osm_polygon_description_tag.runtime.resources"},
    "config": {"osm_polygon_description_tag.runtime.config"},
    "discovery": {"osm_polygon_description_tag.osm.discovery"},
    "extraction": {"osm_polygon_description_tag.osm.extraction"},
    "manifest": {"osm_polygon_description_tag.dataset.manifest"},
    "orchestrator": {
        "osm_polygon_description_tag.workflow.orchestrator",
        "osm_polygon_description_tag.workflow.preflight",
    },
    "pipeline": {"osm_polygon_description_tag.workflow.build"},
    "reporting": {"osm_polygon_description_tag.dataset.reporting"},
    "schema": {"osm_polygon_description_tag.dataset.schema"},
    "storage": {"osm_polygon_description_tag.dataset.storage"},
    "transform": {"osm_polygon_description_tag.dataset.transform"},
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


def _shim_violations(source: str, approved_targets: set[str]) -> list[str]:
    violations: list[str] = []
    tree = ast.parse(source)
    statements = list(tree.body)
    docstring: str | None = None
    imported_targets: set[str] = set()
    runtime_imported_names: set[str] = set()
    public_exports: set[str] = set()
    has_runtime_wildcard = False
    has_type_checking_import = False
    if (
        statements
        and isinstance(statements[0], ast.Expr)
        and isinstance(statements[0].value, ast.Constant)
        and isinstance(statements[0].value.value, str)
    ):
        docstring = statements[0].value.value
        statements.pop(0)
    if docstring is None:
        violations.append("missing module docstring")
    elif not docstring.strip():
        violations.append("empty module docstring")

    all_assignments = 0
    for node in statements:
        if isinstance(node, ast.ImportFrom):
            if node.module == "__future__" and [alias.name for alias in node.names] == [
                "annotations"
            ]:
                continue
            if (
                node.module == "typing"
                and node.level == 0
                and len(node.names) == 1
                and node.names[0].name == "TYPE_CHECKING"
                and node.names[0].asname is None
            ):
                has_type_checking_import = True
                continue
            if node.level or node.module not in approved_targets:
                violations.append(f"unapproved import target: {node.module}")
            else:
                imported_targets.add(node.module)
                runtime_imported_names.update(
                    alias.asname or alias.name for alias in node.names if alias.name != "*"
                )
            if any(alias.name == "*" for alias in node.names):
                violations.append("wildcard import")
                has_runtime_wildcard = True
        elif isinstance(node, ast.If):
            if not isinstance(node.test, ast.Name) or node.test.id != "TYPE_CHECKING":
                violations.append("only TYPE_CHECKING guard is permitted")
                continue
            if not has_type_checking_import:
                violations.append("TYPE_CHECKING guard requires exact typing import")
            if node.orelse:
                violations.append("TYPE_CHECKING guard must not have else")
            for guarded_node in node.body:
                if not isinstance(guarded_node, ast.ImportFrom):
                    violations.append(
                        f"disallowed TYPE_CHECKING statement: {type(guarded_node).__name__}"
                    )
                    continue
                if guarded_node.level or guarded_node.module not in approved_targets:
                    violations.append(
                        f"unapproved TYPE_CHECKING import target: {guarded_node.module}"
                    )
                else:
                    imported_targets.add(guarded_node.module)
                if any(alias.name == "*" for alias in guarded_node.names):
                    violations.append("TYPE_CHECKING wildcard import")
        elif isinstance(node, ast.Assign):
            all_assignments += 1
            valid_target = (
                len(node.targets) == 1
                and isinstance(node.targets[0], ast.Name)
                and node.targets[0].id == "__all__"
            )
            valid_value = isinstance(node.value, ast.List | ast.Tuple) and all(
                isinstance(element, ast.Constant) and isinstance(element.value, str)
                for element in node.value.elts
            )
            if not valid_target or not valid_value:
                violations.append("invalid compatibility metadata")
            elif isinstance(node.value, ast.List | ast.Tuple):
                public_exports.update(element.value for element in node.value.elts)
        else:
            violations.append(f"disallowed statement: {type(node).__name__}")
    if all_assignments != 1:
        violations.append("expected one explicit __all__ assignment")
    if not has_runtime_wildcard:
        violations.extend(
            f"public export lacks unconditional approved import: {name}"
            for name in sorted(public_exports - runtime_imported_names)
        )
    if docstring:
        named_targets = {
            target
            for target in imported_targets
            if re.search(rf"(?<![\w.]){re.escape(target)}(?!\w|\.\w)", docstring)
        }
        if imported_targets and not named_targets:
            violations.append("module docstring does not name approved target")
        else:
            violations.extend(
                f"module docstring missing approved target: {target}"
                for target in sorted(imported_targets - named_targets)
            )
    return violations


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


@pytest.mark.parametrize(
    ("source", "expected_violation"),
    [
        (
            '''"""Compatibility imports for osm_polygon_description_tag.runtime.logging."""

from osm_polygon_description_tag.runtime.logging import *

__all__ = ["RunLogger"]
''',
            "wildcard import",
        ),
        (
            '''"""Compatibility imports for osm_polygon_description_tag.runtime.logging."""

import subprocess
from osm_polygon_description_tag.runtime.logging import RunLogger

__all__ = ["RunLogger"]
''',
            "disallowed statement: Import",
        ),
    ],
)
def test_shim_mutations_are_rejected(source: str, expected_violation: str) -> None:
    assert _shim_violations(source, {"osm_polygon_description_tag.runtime.logging"}) == [
        expected_violation
    ]


@pytest.mark.parametrize(
    ("source", "expected_violation"),
    [
        (
            """from osm_polygon_description_tag.runtime.logging import RunLogger

__all__ = ["RunLogger"]
""",
            "missing module docstring",
        ),
        (
            '''"""Compatibility imports for the wrong module."""

from osm_polygon_description_tag.runtime.logging import RunLogger

__all__ = ["RunLogger"]
''',
            "module docstring does not name approved target",
        ),
    ],
)
def test_shim_docstring_mutations_are_rejected(source: str, expected_violation: str) -> None:
    assert _shim_violations(source, {"osm_polygon_description_tag.runtime.logging"}) == [
        expected_violation
    ]


def test_shim_permits_narrow_type_checking_imports() -> None:
    source = '''"""Compatibility imports for osm_polygon_description_tag.runtime.logging."""

from typing import TYPE_CHECKING

from osm_polygon_description_tag.runtime.logging import RunLogger

if TYPE_CHECKING:
    from osm_polygon_description_tag.runtime.logging import configure_rotation

__all__ = ["RunLogger"]
'''
    assert _shim_violations(source, {"osm_polygon_description_tag.runtime.logging"}) == []


def test_type_checking_only_import_is_valid_when_not_publicly_exported() -> None:
    source = '''"""Compatibility imports for osm_polygon_description_tag.runtime.logging."""

from typing import TYPE_CHECKING

from osm_polygon_description_tag.runtime.logging import configure_rotation

if TYPE_CHECKING:
    from osm_polygon_description_tag.runtime.logging import RunLogger

__all__ = ["configure_rotation"]
'''
    assert _shim_violations(source, {"osm_polygon_description_tag.runtime.logging"}) == []


def test_type_checking_only_import_cannot_satisfy_public_export() -> None:
    source = '''"""Compatibility imports for osm_polygon_description_tag.runtime.logging."""

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from osm_polygon_description_tag.runtime.logging import RunLogger

__all__ = ["RunLogger"]
'''
    assert _shim_violations(source, {"osm_polygon_description_tag.runtime.logging"}) == [
        "public export lacks unconditional approved import: RunLogger"
    ]


@pytest.mark.parametrize(
    ("mutation", "expected_violation"),
    [
        ("if True:\n    pass", "only TYPE_CHECKING guard is permitted"),
        (
            "if TYPE_CHECKING:\n    reveal_type(object())",
            "disallowed TYPE_CHECKING statement: Expr",
        ),
        (
            "if TYPE_CHECKING:\n    from osm_polygon_description_tag.runtime.logging import *",
            "TYPE_CHECKING wildcard import",
        ),
        (
            "if TYPE_CHECKING:\n    from osm_polygon_description_tag.dataset.schema import SCHEMA",
            "unapproved TYPE_CHECKING import target: osm_polygon_description_tag.dataset.schema",
        ),
        (
            "if TYPE_CHECKING:\n"
            "    from osm_polygon_description_tag.runtime.logging import RunLogger\n"
            "else:\n"
            "    pass",
            "TYPE_CHECKING guard must not have else",
        ),
    ],
)
def test_shim_type_checking_mutations_are_rejected(mutation: str, expected_violation: str) -> None:
    source = f'''"""Compatibility imports for osm_polygon_description_tag.runtime.logging."""

from typing import TYPE_CHECKING

from osm_polygon_description_tag.runtime.logging import RunLogger

{mutation}

__all__ = ["RunLogger"]
'''
    assert _shim_violations(source, {"osm_polygon_description_tag.runtime.logging"}) == [
        expected_violation
    ]


def test_multi_target_shim_docstring_must_name_every_imported_target() -> None:
    source = '''"""Compatibility imports for osm_polygon_description_tag.workflow.orchestrator."""

from osm_polygon_description_tag.workflow.orchestrator import run_and_publish
from osm_polygon_description_tag.workflow.preflight import PreflightError

__all__ = ["PreflightError", "run_and_publish"]
'''
    assert _shim_violations(
        source,
        {
            "osm_polygon_description_tag.workflow.orchestrator",
            "osm_polygon_description_tag.workflow.preflight",
        },
    ) == [
        "module docstring missing approved target: osm_polygon_description_tag.workflow.preflight"
    ]


def test_docstring_near_prefix_is_not_an_exact_canonical_target() -> None:
    source = '''"""Compatibility imports for osm_polygon_description_tag.runtime.logging_extra."""

from osm_polygon_description_tag.runtime.logging import RunLogger

__all__ = ["RunLogger"]
'''
    assert _shim_violations(source, {"osm_polygon_description_tag.runtime.logging"}) == [
        "module docstring does not name approved target"
    ]


@pytest.mark.parametrize("module_name", COMPATIBILITY_MODULES)
def test_compatibility_module_is_a_pure_import_shim(module_name: str) -> None:
    path = PACKAGE_ROOT / f"{module_name}.py"
    assert _shim_violations(path.read_text(encoding="utf-8"), SHIM_TARGETS[module_name]) == []
