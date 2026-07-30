"""Deterministic, read-only enumeration of source PBF inputs."""

from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class Source:
    path: Path
    name: str
    output_name: str
    size_bytes: int
    mtime_ns: int


def _output_name_for(source_name: str) -> str:
    return f"{source_name.removesuffix('.osm.pbf')}.parquet"


def discover_sources(source_root: Path) -> tuple[Source, ...]:
    if not source_root.is_dir():
        raise NotADirectoryError(source_root)
    paths = sorted(source_root.glob("*.osm.pbf"), key=lambda path: path.name)
    result: list[Source] = []
    output_names: set[str] = set()
    for path in paths:
        # Reject symlinks so discovery never follows indirect or unsafe inputs.
        if path.is_symlink() or not path.is_file():
            continue
        output_name = _output_name_for(path.name)
        # Defensive: distinct direct children map injectively, but the guard pins
        # the output-uniqueness contract against future naming changes.
        if output_name in output_names:
            raise ValueError(f"output collision: {output_name}")
        stat = path.stat()
        result.append(Source(path, path.name, output_name, stat.st_size, stat.st_mtime_ns))
        output_names.add(output_name)
    return tuple(result)
