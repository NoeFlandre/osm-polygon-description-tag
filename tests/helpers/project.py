"""Builders for the minimal synthetic project tree staged by grid tests."""

from __future__ import annotations

from pathlib import Path


def write_project(project: Path, *, readme: bool = False) -> None:
    """Write a tiny project with sources, ``pyproject.toml`` and ``uv.lock``."""
    (project / "src").mkdir(parents=True)
    (project / "src" / "module.py").write_text("VALUE = 1\n", encoding="utf-8")
    (project / "pyproject.toml").write_text("[project]\nname = 'synthetic'\n", encoding="utf-8")
    (project / "uv.lock").write_text("version = 1\n", encoding="utf-8")
    if readme:
        (project / "README.md").write_text("# synthetic\n", encoding="utf-8")
