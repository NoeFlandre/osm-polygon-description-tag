import tomllib
from pathlib import Path

ROOT = Path(__file__).parents[2]


def test_public_uv_project_foundation_is_complete() -> None:
    pyproject_path = ROOT / "pyproject.toml"
    readme_path = ROOT / "README.md"
    package_root = ROOT / "src" / "osm_polygon_description_tag"

    assert pyproject_path.is_file()
    assert readme_path.is_file()
    assert (ROOT / ".gitignore").is_file()
    assert (package_root / "__init__.py").is_file()
    assert (package_root / "py.typed").is_file()

    project = tomllib.loads(pyproject_path.read_text(encoding="utf-8"))["project"]
    assert project["name"] == "osm-polygon-description-tag"
    assert project["requires-python"] == ">=3.12"
    assert project["license"] == "Apache-2.0"
    assert project["scripts"] == {
        "osm-polygon-description-tag": "osm_polygon_description_tag.cli:main"
    }

    readme = readme_path.read_text(encoding="utf-8")
    assert readme.startswith("# OSM Polygon Description Tag")
    assert "Open Database License" in readme
    assert "No real source PBF has been processed" in readme


def test_foundation_excludes_external_data_from_git() -> None:
    ignored = (ROOT / ".gitignore").read_text(encoding="utf-8").splitlines()

    assert "data/" not in ignored
    assert "*.osm.pbf" in ignored
    assert "*.parquet" in ignored
