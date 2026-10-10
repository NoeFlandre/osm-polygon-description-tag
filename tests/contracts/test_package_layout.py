import importlib
import os
import tomllib
from pathlib import Path

import pytest

import osm_polygon_description_tag

STAGED_PACKAGES = ("runtime", "osm", "dataset", "workflow")
CANONICAL_PACKAGES = (*STAGED_PACKAGES, "publication")
STATS_MODULES = ("stats", "stats_features", "stats_geometry", "stats_manifest")
MAX_DATASET_MODULE_LINES = 600


def test_stats_is_split_into_modules_under_the_size_bound() -> None:
    if "MUTANT_UNDER_TEST" in os.environ:
        pytest.skip("static architecture bounds are checked on the canonical source tree")
    dataset_root = Path(osm_polygon_description_tag.__file__).parent / "dataset"
    for module in STATS_MODULES:
        path = dataset_root / f"{module}.py"
        assert path.is_file(), path
        assert len(path.read_text(encoding="utf-8").splitlines()) <= MAX_DATASET_MODULE_LINES, path


def test_every_dataset_module_stays_under_the_size_bound() -> None:
    if "MUTANT_UNDER_TEST" in os.environ:
        pytest.skip("static architecture bounds are checked on the canonical source tree")
    dataset_root = Path(osm_polygon_description_tag.__file__).parent / "dataset"
    oversized = {
        str(path.relative_to(dataset_root)): len(path.read_text(encoding="utf-8").splitlines())
        for path in sorted(dataset_root.rglob("*.py"))
        if len(path.read_text(encoding="utf-8").splitlines()) > MAX_DATASET_MODULE_LINES
    }
    assert oversized == {}


@pytest.mark.parametrize(
    ("name", "module"),
    [
        ("collect_stats", "stats"),
        ("ReportingError", "stats_manifest"),
        ("ManifestSummary", "stats_manifest"),
        ("FeatureSummary", "stats_features"),
        ("collect_spatial_summary", "stats_geometry"),
    ],
)
def test_each_stats_name_is_defined_by_the_module_that_owns_it(name: str, module: str) -> None:
    owner = importlib.import_module(f"osm_polygon_description_tag.dataset.{module}")
    assert getattr(owner, name).__module__ == owner.__name__


def test_dataset_stats_keeps_its_public_names_importable_from_its_path() -> None:
    stats = importlib.import_module("osm_polygon_description_tag.dataset.stats")
    for name in (
        "collect_stats",
        "ReportingError",
        "STATS_SCHEMA_VERSION",
        "TEXT_REJECTION_REASONS",
    ):
        assert hasattr(stats, name), name


def test_unit_tests_mirror_source_domains() -> None:
    project_root = Path(osm_polygon_description_tag.__file__).parent.parents[1]
    tests_root = project_root / "tests"
    assert not list(tests_root.glob("test_*.py"))
    for domain in CANONICAL_PACKAGES:
        assert (tests_root / "unit" / domain).is_dir()


def test_source_distribution_excludes_runtime_artifacts() -> None:
    project_root = Path(osm_polygon_description_tag.__file__).parents[2]
    project = tomllib.loads((project_root / "pyproject.toml").read_text(encoding="utf-8"))
    target = project["tool"]["hatch"]["build"]["targets"]["sdist"]
    excluded = set(target["exclude"])

    assert {
        ".venv/",
        "data-root/",
        "dist/",
        "mutants/",
        "reports/",
    } <= excluded
    assert target["skip-excluded-dirs"] is True
