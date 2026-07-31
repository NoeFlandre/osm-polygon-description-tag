import tomllib
from pathlib import Path

import yaml

ROOT = Path(__file__).parents[2]
PUBLIC_PAGES = (
    "index.md",
    "getting-started.md",
    "dataset-contract.md",
    "operations.md",
    "cli.md",
    "development.md",
    "architecture.md",
)


def test_mkdocs_site_has_public_navigation_and_tooling() -> None:
    config_path = ROOT / "mkdocs.yml"
    config = yaml.safe_load(config_path.read_text(encoding="utf-8"))
    project = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    dev_dependencies = project["dependency-groups"]["dev"]

    assert config["site_name"] == "OSM Polygon Description Tag"
    assert config["theme"]["name"] == "material"
    assert "mkdocs-material>=9.6,<10" in dev_dependencies
    assert config["strict"] is True
    assert "superpowers/*" in config["exclude_docs"]

    nav_pages = {
        page
        for item in config["nav"]
        for page in (item.values() if isinstance(item, dict) else ())
        if isinstance(page, str) and page.endswith(".md")
    }
    assert set(PUBLIC_PAGES) <= nav_pages
    for page in PUBLIC_PAGES:
        assert (ROOT / "docs" / page).is_file()


def test_public_docs_state_operational_boundaries() -> None:
    text = "\n".join((ROOT / "docs" / page).read_text(encoding="utf-8") for page in PUBLIC_PAGES)

    assert "/Volumes/Seagate M3/projects/osm-polygon-wikidata-only/raw" in text
    assert "/Volumes/Seagate M3/projects/osm-polygon-description-tag" in text
    assert "just run-and-publish" in text
    assert "Hugging Face" in text
    assert "area_m2" in text
    assert "localized_descriptions" in text
