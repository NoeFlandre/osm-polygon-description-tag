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


def test_github_pages_workflow_deploys_the_strict_site() -> None:
    workflow = (ROOT / ".github" / "workflows" / "docs.yml").read_text(encoding="utf-8")

    assert "branches: [main]" in workflow
    assert "uv run mkdocs build --strict --site-dir site" in workflow
    assert "actions/configure-pages@983d7736d9b0ae728b81ab479565c72886d7745b" in workflow
    assert "actions/upload-pages-artifact@7b1f4a764d45c48632c6b24a0339c27f5614fb0b" in workflow
    assert "actions/deploy-pages@d6db90164ac5ed86f2b6aed7e0febac5b3c0c03e" in workflow
    assert "pages: write" in workflow
    assert "id-token: write" in workflow
    assert "environment:\n      name: github-pages" in workflow
