"""Typed, immutable request objects handed from Typer commands to their handlers."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from osm_polygon_description_tag.runtime.presentation import TerminalPresenter

# The stderr threshold when neither -v nor -q is given.
DEFAULT_STDERR_LEVEL = "INFO"


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


@dataclass(frozen=True, slots=True)
class MigrateTextRequest(PathOptions):
    """``migrate-text``: the shared options plus how many artifacts to repair at once."""

    max_workers: int


@dataclass(frozen=True, slots=True)
class TrackioSnapshotRequest(PathOptions):
    """``trackio-snapshot``: the shared options plus where the snapshot is logged."""

    project: str
    space_id: str
    run_name: str | None


@dataclass(frozen=True, slots=True)
class PublishRequest(PathOptions):
    """``publish``: the shared options plus the plan identity the operator confirmed."""

    plan: str


@dataclass(frozen=True, slots=True)
class ReleaseStatsRequest(PathOptions):
    """``release-stats``: the shared options plus the repo confirmation and the apply gate."""

    confirm_repo: str
    apply: bool


@dataclass(frozen=True, slots=True)
class RunAndPublishRequest(PathOptions):
    """``run-and-publish``: the shared options plus the confirmation and terminal output.

    ``presenter`` is ``None`` when no terminal is attached; the run then creates no
    event logger. ``stderr_level`` is the threshold chosen with -v / -q.
    """

    confirm_repo: str
    presenter: TerminalPresenter | None = None
    stderr_level: str = DEFAULT_STDERR_LEVEL


__all__ = [
    "DEFAULT_STDERR_LEVEL",
    "BuildOneRequest",
    "MigrateTextRequest",
    "PathOptions",
    "PublishRequest",
    "ReleaseStatsRequest",
    "RunAndPublishRequest",
    "TrackioSnapshotRequest",
]
