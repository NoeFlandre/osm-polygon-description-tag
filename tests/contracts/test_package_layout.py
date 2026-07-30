from pathlib import Path

import osm_polygon_description_tag

STAGED_PACKAGES = ("runtime", "osm", "dataset", "workflow")
CANONICAL_PACKAGES = (*STAGED_PACKAGES, "publication")
README_SECTIONS = (
    "## Purpose",
    "## Responsibilities",
    "## Non-responsibilities",
    "## Public API",
    "## Allowed dependencies",
    "## Data flow and side effects",
    "## Safety and determinism invariants",
    "## Tests",
)


def test_canonical_packages_are_documented() -> None:
    package_root = Path(osm_polygon_description_tag.__file__).parent
    for package_name in STAGED_PACKAGES:
        package = package_root / package_name
        assert (package / "__init__.py").is_file()
        readme = (package / "README.md").read_text(encoding="utf-8")
        for section in README_SECTIONS:
            assert section in readme


def test_package_and_project_architecture_docs_exist() -> None:
    package_root = Path(osm_polygon_description_tag.__file__).parent
    project_root = package_root.parents[1]
    assert (package_root / "README.md").is_file()
    assert (project_root / "docs" / "architecture.md").is_file()
    assert (project_root / "docs" / "development.md").is_file()


def test_unit_tests_mirror_source_domains() -> None:
    project_root = Path(osm_polygon_description_tag.__file__).parent.parents[1]
    tests_root = project_root / "tests"
    assert not list(tests_root.glob("test_*.py"))
    for domain in CANONICAL_PACKAGES:
        assert (tests_root / "unit" / domain).is_dir()
