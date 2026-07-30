"""Resolve packaged resources and locate the project checkout.

Resource files (config/osmium-export.json and docs/dataset-card-template.md) are
copied into ``osm_polygon_description_tag/_data/`` at build time and resolved
through :func:`importlib.resources.files` regardless of the caller's working
directory. The code revision is read from the project checkout via the
``OSM_POLYGON_DESCRIPTION_TAG_HOME`` environment variable when set, falling
back to walking up from this file to find a Git checkout.
"""

from __future__ import annotations

import os
import subprocess
from pathlib import Path

_PACKAGE_NAME = "osm_polygon_description_tag"


def package_data_dir() -> Path:
    """Return the directory containing the packaged config and template files."""
    from importlib.resources import files

    return Path(str(files(_PACKAGE_NAME).joinpath("_data")))


def resource_path(name: str) -> Path:
    """Return the absolute path of a packaged resource by filename."""
    candidate = package_data_dir() / name
    if not candidate.is_file():
        raise FileNotFoundError(f"packaged resource missing: {name}")
    return candidate


def osmium_export_config() -> Path:
    return resource_path("osmium-export.json")


def dataset_card_template() -> Path:
    return resource_path("dataset-card-template.md")


def project_root() -> Path:
    """Locate the project checkout root from the installed package location.

    Resolution order:

    1. ``OSM_POLYGON_DESCRIPTION_TAG_HOME`` environment variable if set and
       pointing at a directory that contains a ``pyproject.toml``.
    2. Walk upward from this package file looking for ``pyproject.toml`` so
       editable installs and source checkouts both work.
    """
    env = os.environ.get("OSM_POLYGON_DESCRIPTION_TAG_HOME")
    if env:
        root = Path(env).resolve(strict=False)
        if (root / "pyproject.toml").is_file():
            return root
    here = Path(__file__).resolve(strict=False)
    for parent in (here, *here.parents):
        if (parent / "pyproject.toml").is_file():
            return parent
    raise FileNotFoundError("project root with pyproject.toml not found")


def project_code_revision() -> str | None:
    """Return the Git revision of the project checkout, or ``None``."""
    try:
        root = project_root()
    except FileNotFoundError:
        return None
    import shutil

    git_executable = shutil.which("git") or "git"
    try:
        completed = subprocess.run(  # noqa: S603 - controlled args, no shell
            [git_executable, "-C", str(root), "rev-parse", "HEAD"],
            check=False,
            capture_output=True,
            text=True,
            timeout=5,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    if completed.returncode != 0:
        return None
    revision = completed.stdout.strip()
    return revision or None
