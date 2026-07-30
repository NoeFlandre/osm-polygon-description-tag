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
        if not directory.is_dir() or directory.is_symlink():
            continue
        for candidate in directory.iterdir():
            match = _OWNED_TEMP_PATTERN.fullmatch(candidate.name)
            if match is None or candidate.is_symlink() or not candidate.is_file():
                continue
            target_name = match.group("target")
            if exact_targets is not None and target_name not in exact_targets:
                continue
            if directory.name == "data" and not target_name.endswith(".parquet"):
                continue
            if directory.name == "manifests" and not target_name.endswith(".manifest.json"):
                continue
            target = directory / target_name
            if target.is_symlink() or not target.is_file():
                continue
            if candidate.stat().st_mtime_ns >= target.stat().st_mtime_ns:
                continue
            candidate.unlink()
            removed.append(candidate)
    return tuple(sorted(removed, key=lambda path: str(path)))


__all__ = ["cleanup_stale_owned_temps"]
