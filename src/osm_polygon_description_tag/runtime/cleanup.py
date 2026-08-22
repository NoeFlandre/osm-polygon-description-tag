"""Safe cleanup of abandoned application-owned temporary files."""

from __future__ import annotations

import re
from pathlib import Path

_OWNED_TEMP_PATTERN = re.compile(r"^\.(?P<target>.+)\.[0-9a-f]{32}\.tmp$")


def cleanup_stale_owned_temps(data_root: Path) -> tuple[Path, ...]:
    """Remove abandoned owned temps only when a newer finalized target exists."""
    locations = (
        (data_root, {"README.md", "stats.json", "publication-state.json"}),
        (data_root / "data", None),
        (data_root / "manifests", None),
    )
    removed: list[Path] = []
    for directory, exact_targets in locations:
        removed.extend(_cleanup_directory(directory, exact_targets))
    # pragma: no mutate start - all returned paths use the three fixed locations
    ordered = sorted(removed, key=lambda path: str(path))
    # pragma: no mutate end
    return tuple(ordered)


def _cleanup_directory(directory: Path, exact_targets: set[str] | None) -> tuple[Path, ...]:
    if not directory.is_dir() or directory.is_symlink():
        return ()
    removed: list[Path] = []
    for candidate in directory.iterdir():
        removed_candidate = _remove_if_stale(directory, candidate, exact_targets)
        if removed_candidate is not None:
            removed.append(removed_candidate)
    return tuple(removed)


def _eligible_target(
    directory: Path,
    candidate: Path,
    exact_targets: set[str] | None,
) -> Path | None:
    target_name = _candidate_target_name(candidate, exact_targets)
    if target_name is None or not _target_name_allowed(directory, target_name):
        return None
    target = directory / target_name
    if target.is_symlink() or not target.is_file():
        return None
    return target


def _candidate_target_name(candidate: Path, exact_targets: set[str] | None) -> str | None:
    if not _regular_file(candidate):
        return None
    target_name = _extract_target_name(candidate)
    if target_name is None:
        return None
    if exact_targets is not None and target_name not in exact_targets:
        return None
    return target_name


def _regular_file(path: Path) -> bool:
    return not path.is_symlink() and path.is_file()


def _extract_target_name(candidate: Path) -> str | None:
    match = _OWNED_TEMP_PATTERN.fullmatch(candidate.name)
    return match.group("target") if match is not None else None


def _target_name_allowed(directory: Path, target_name: str) -> bool:
    if directory.name == "data":
        return target_name.endswith(".parquet")
    if directory.name == "manifests":
        return target_name.endswith(".manifest.json")
    return True


def _remove_if_stale(
    directory: Path,
    candidate: Path,
    exact_targets: set[str] | None,
) -> Path | None:
    target = _eligible_target(directory, candidate, exact_targets)
    if target is None or not _target_is_newer(candidate, target):
        return None
    candidate.unlink()
    return candidate


def _target_is_newer(candidate: Path, target: Path) -> bool:
    return candidate.stat().st_mtime_ns < target.stat().st_mtime_ns


__all__ = ["cleanup_stale_owned_temps"]
