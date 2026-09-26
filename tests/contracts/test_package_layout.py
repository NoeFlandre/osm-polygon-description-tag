import tomllib
from pathlib import Path

import osm_polygon_description_tag

STAGED_PACKAGES = ("runtime", "osm", "dataset", "workflow")
CANONICAL_PACKAGES = (*STAGED_PACKAGES, "publication")


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
