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
    for package_name in CANONICAL_PACKAGES:
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


def test_unused_top_level_deduplication_shim_is_not_packaged() -> None:
    """Deduplication has one canonical module under the dataset package."""
    package_root = Path(osm_polygon_description_tag.__file__).parent
    assert not (package_root / "deduplication.py").exists()


def test_active_docs_name_the_complete_toolchain() -> None:
    package_root = Path(osm_polygon_description_tag.__file__).parent
    project_root = package_root.parents[1]
    paths = (
        project_root / "README.md",
        project_root / "docs" / "development.md",
        project_root / "docs" / "architecture.md",
    )
    text = "\n".join(path.read_text(encoding="utf-8") for path in paths)

    for name in (
        "uv",
        "Ruff",
        "ty",
        "pytest",
        "pre-commit",
        "Typer",
        "Rich",
        "tqdm",
        "Just",
        "GitHub Actions",
    ):
        assert name in text
    assert "uv run mypy" not in text
    assert "argparse" not in text
