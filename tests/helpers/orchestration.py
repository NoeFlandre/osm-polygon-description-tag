"""Synthetic filesystem and exporter fixtures for orchestrator tests."""

from __future__ import annotations

import json
from collections.abc import Callable, Iterator
from pathlib import Path

from shapely import to_wkb
from shapely.geometry import Polygon

from osm_polygon_description_tag.osm.discovery import Source
from osm_polygon_description_tag.osm.extraction import ExportRecord
from osm_polygon_description_tag.runtime.config import Paths


class RecordingLogger:
    """Capture orchestrator logger events and preflight decisions."""

    def __init__(self) -> None:
        self.events: list[tuple[str, dict[str, object]]] = []
        self.flushed = False
        self.preflight_approved = False
        self.preflight_denied = False

    def event(self, name: str, **fields: object) -> None:
        self.events.append((name, fields))

    def flush(self) -> None:
        self.flushed = True

    def approve_preflight(self) -> None:
        self.preflight_approved = True

    def deny_preflight(self) -> None:
        self.preflight_denied = True


class RecordingTracker:
    """Capture the tracker snapshots, starts, and logged payloads."""

    def __init__(self) -> None:
        self.snapshots: list[Path] = []
        self.starts: list[dict[str, object]] = []
        self.logs: list[dict[str, object]] = []

    def log_snapshot(self, data_root: Path) -> None:
        self.snapshots.append(data_root)

    def start(self, *, config: dict[str, object]) -> None:
        self.starts.append(config)

    def log(self, data: dict[str, object]) -> None:
        self.logs.append(data)


Exporter = Callable[[Path, Path], Iterator[ExportRecord]]


def setup_workspace(tmp_path: Path) -> tuple[Paths, Path, Path]:
    """Create the raw/generated roots and one synthetic source PBF."""
    source_root = tmp_path / "raw"
    data_root = tmp_path / "generated"
    source_root.mkdir()
    data_root.mkdir()
    (source_root / "a.osm.pbf").write_bytes(b"a-bytes")
    return Paths(source_root=source_root, data_root=data_root), source_root, data_root


def setup_two_source_workspace(tmp_path: Path) -> tuple[Paths, Path, Path]:
    """Create raw/generated roots with the paired alpha/beta source inputs."""
    source_root = tmp_path / "raw"
    data_root = tmp_path / "generated"
    source_root.mkdir()
    data_root.mkdir()
    (source_root / "a.osm.pbf").write_bytes(b"a-bytes")
    (source_root / "b.osm.pbf").write_bytes(b"b-bytes")
    return Paths(source_root=source_root, data_root=data_root), source_root, data_root


def source_runner_workspace(tmp_path: Path) -> tuple[Paths, Source]:
    """Create the file-backed source and output expected by runner tests."""
    source_root = tmp_path / "raw"
    data_root = tmp_path / "generated"
    source_root.mkdir()
    (data_root / "data").mkdir(parents=True)
    (data_root / "manifests").mkdir()
    source_path = source_root / "region.osm.pbf"
    source_path.write_bytes(b"source")
    output_path = data_root / "data" / "region.parquet"
    output_path.write_bytes(b"parquet")
    return Paths(source_root=source_root, data_root=data_root), Source(
        path=source_path,
        name=source_path.name,
        output_name=output_path.name,
        size_bytes=source_path.stat().st_size,
        mtime_ns=source_path.stat().st_mtime_ns,
    )


def preflight_paths(tmp_path: Path) -> Paths:
    """Create one source and an empty generated root for preflight checks."""
    source_root = tmp_path / "raw"
    data_root = tmp_path / "generated"
    source_root.mkdir()
    data_root.mkdir()
    (source_root / "a.osm.pbf").write_bytes(b"a-bytes")
    return Paths(source_root=source_root, data_root=data_root)


def fake_exporter() -> Exporter:
    """Return a one-feature exporter whose identifier follows the source filename."""

    def export(source_path: Path, _config_path: Path) -> Iterator[ExportRecord]:
        stem = source_path.name.removesuffix(".osm.pbf")
        osm_id = abs(hash(stem)) % 1000000
        geometry = Polygon([(0, 0), (0, 1), (1, 1), (1, 0)])
        ewkb = to_wkb(geometry, include_srid=True, flavor="extended", byte_order=1)
        record = ExportRecord(
            geometry_ewkb_hex=ewkb.hex(),
            osm_type="way",
            osm_id=osm_id,
            version=1,
            changeset=1,
            timestamp="2026-01-01T00:00:00Z",
            tags=json.loads('{"description": "x"}'),
        )
        return iter([record])

    return export
