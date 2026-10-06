"""Typed, immutable request objects handed from Typer commands to their handlers."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True, slots=True)
class PathOptions:
    """The root and executable options shared by the local dataset commands."""

    source_root: Path | None
    data_root: Path | None
    osmium: str


@dataclass(frozen=True, slots=True)
class BuildOneRequest(PathOptions):
    """``build-one``: the shared options plus the basename of the source to build."""

    basename: str


__all__ = ["BuildOneRequest", "PathOptions"]
