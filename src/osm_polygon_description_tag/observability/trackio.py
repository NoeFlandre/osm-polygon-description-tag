"""Optional Trackio metrics for retrospective and live pipeline runs.

Trackio is deliberately isolated behind :class:`TrackioRecorder`.  A missing
installation, missing Hugging Face credentials, or a dashboard outage disables
metrics and never fails extraction, validation, resumability, or publication.
When enabled, local Trackio data lives below ``<data-root>/logs/trackio`` and
the configured Space receives the same metrics for a persistent dashboard.
"""

from __future__ import annotations

import hashlib
import importlib
import json
import os
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Protocol, cast
from urllib.parse import quote

from osm_polygon_description_tag.dataset.reporting import collect_stats

DEFAULT_TRACKIO_PROJECT = "osm-polygon-description-tag"
DEFAULT_TRACKIO_SPACE_ID = "NoeFlandre/osm-polygon-description-tag-trackio"
TRACKIO_DASHBOARD_URL = (
    "https://noeflandre-osm-polygon-description-tag-trackio.hf.space/"
    "?project=osm-polygon-description-tag&sidebar=hidden"
)


class TrackioBackend(Protocol):
    """Minimal module surface used by the recorder and tests."""

    def init(self, **kwargs: object) -> object: ...

    def log(self, metrics: dict[str, object], step: int | None = None) -> None: ...

    def finish(self) -> None: ...


def dashboard_url(
    project: str = DEFAULT_TRACKIO_PROJECT,
    space_id: str = DEFAULT_TRACKIO_SPACE_ID,
) -> str:
    """Return a stable public dashboard URL for a project in a Space."""
    owner, _, name = space_id.partition("/")
    if not owner or not name:
        raise ValueError(f"space_id must be '<owner>/<name>': {space_id!r}")
    host = f"{owner}-{name}".lower().replace("_", "-")
    return f"https://{host}.hf.space/?project={quote(project, safe='-_.~')}&sidebar=hidden"


def retrospective_run_name(stats_sha256: str) -> str:
    """Return a stable run name for one exact dataset snapshot."""
    if len(stats_sha256) < 12:
        raise ValueError("stats_sha256 must contain at least 12 characters")
    return f"retrospective-{stats_sha256[:12]}"


def _integer(value: object) -> int:
    return int(value) if isinstance(value, int | float) else 0


def build_retrospective_points(stats: Mapping[str, Any]) -> list[dict[str, object]]:
    """Build deterministic cumulative per-file metrics from ``stats.json``."""
    files = stats.get("files", [])
    if not isinstance(files, Sequence) or isinstance(files, str | bytes | bytearray):
        return []
    ordered = sorted(
        (entry for entry in files if isinstance(entry, Mapping)),
        key=lambda entry: str(entry.get("parquet", "")),
    )
    cumulative_rows = 0
    cumulative_output_bytes = 0
    cumulative_source_bytes = 0
    points: list[dict[str, object]] = []
    for step, entry in enumerate(ordered, start=1):
        source_rows = _integer(entry.get("rows"))
        source_output_bytes = _integer(entry.get("output_bytes"))
        source_bytes = _integer(entry.get("source_bytes"))
        cumulative_rows += source_rows
        cumulative_output_bytes += source_output_bytes
        cumulative_source_bytes += source_bytes
        points.append(
            {
                "step": step,
                "source_rows": source_rows,
                "cumulative_rows": cumulative_rows,
                "source_output_bytes": source_output_bytes,
                "cumulative_output_bytes": cumulative_output_bytes,
                "source_bytes": source_bytes,
                "cumulative_source_bytes": cumulative_source_bytes,
            }
        )
    return points


def _load_backend() -> TrackioBackend | None:
    try:
        return cast(TrackioBackend, importlib.import_module("trackio"))
    except (ImportError, OSError):
        return None


@dataclass
class TrackioRecorder:
    """Failure-isolated Trackio run with data-root-local persistence."""

    data_root: Path
    backend: TrackioBackend | None = None
    project: str = DEFAULT_TRACKIO_PROJECT
    space_id: str = DEFAULT_TRACKIO_SPACE_ID
    run_name: str | None = None
    enabled: bool = False
    started_successfully: bool = False
    failure_reason: str | None = None
    _started: bool = False

    def start(self, *, config: Mapping[str, object]) -> bool:
        """Start a run after pipeline preflight; return whether it is active."""
        if self._started:
            return self.enabled
        self._started = True
        if self.backend is None and os.environ.get(
            "OSM_POLYGON_DESCRIPTION_TAG_TRACKIO", "1"
        ).lower() in {"0", "false", "off", "no"}:
            self.failure_reason = "trackio disabled by environment"
            return False
        backend = self.backend or _load_backend()
        if backend is None:
            self.failure_reason = "trackio is not installed"
            return False
        try:
            trackio_dir = self.data_root / "logs" / "trackio"
            trackio_dir.mkdir(parents=True, exist_ok=True)
            os.environ["TRACKIO_DIR"] = str(trackio_dir)
            kwargs: dict[str, object] = {
                "project": self.project,
                "space_id": self.space_id,
                "config": dict(config),
            }
            if self.run_name is not None:
                kwargs["name"] = self.run_name
            backend.init(**kwargs)
        except Exception as error:  # Trackio must never take down the pipeline.
            self.failure_reason = f"trackio initialization failed: {error}"
            return False
        self.backend = backend
        self.enabled = True
        self.started_successfully = True
        return True

    def log(self, metrics: Mapping[str, object]) -> None:
        """Queue numeric metrics, disabling the sink if Trackio rejects them."""
        if not self.enabled or self.backend is None:
            return
        try:
            payload = dict(metrics)
            step = payload.pop("step", None)
            self.backend.log(payload, step=int(step) if isinstance(step, int | float) else None)
        except Exception as error:  # pragma: no cover - defensive integration boundary
            self.failure_reason = f"trackio logging failed: {error}"
            self.enabled = False

    def finish(self) -> None:
        """Flush and close the run without propagating Trackio failures."""
        if not self._started or self.backend is None:
            return
        try:
            self.backend.finish()
        except Exception as error:  # pragma: no cover - defensive integration boundary
            self.failure_reason = f"trackio finish failed: {error}"
        finally:
            self.enabled = False


@dataclass(frozen=True)
class TrackioReport:
    """Machine-readable result of a retrospective logging operation."""

    project: str
    space_id: str
    run_name: str
    dashboard_url: str
    enabled: bool
    point_count: int
    failure_reason: str | None

    def to_payload(self) -> dict[str, object]:
        return {
            "project": self.project,
            "space_id": self.space_id,
            "run_name": self.run_name,
            "dashboard_url": self.dashboard_url,
            "enabled": self.enabled,
            "point_count": self.point_count,
            "failure_reason": self.failure_reason,
        }


def _stats_sha256(stats: Mapping[str, Any]) -> str:
    encoded = json.dumps(stats, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


def publish_retrospective(
    data_root: Path,
    *,
    backend: TrackioBackend | None = None,
    project: str = DEFAULT_TRACKIO_PROJECT,
    space_id: str = DEFAULT_TRACKIO_SPACE_ID,
) -> TrackioReport:
    """Log one completed dataset snapshot and its per-file metric curve."""
    stats = collect_stats(data_root)
    stats_sha256 = _stats_sha256(stats)
    run_name = retrospective_run_name(stats_sha256)
    recorder = TrackioRecorder(
        data_root=data_root,
        backend=backend,
        project=project,
        space_id=space_id,
        run_name=run_name,
    )
    points = build_retrospective_points(stats)
    config = {
        "dataset_repo": "NoeFlandre/osm-polygon-description-tag",
        "dataset_stats_sha256": stats_sha256,
        "schema_version": _integer(stats.get("schema_version")),
        "source_count": len(points),
    }
    recorder.start(config=config)
    for point in points:
        recorder.log(point)
    recorder.log(
        {
            "step": len(points) + 1,
            "dataset_rows": _integer(stats.get("rows")),
            "dataset_output_bytes": _integer(stats.get("output_bytes_total")),
            "dataset_source_bytes": _integer(stats.get("source_bytes_total")),
            "base_description_values": _integer(stats.get("base_description_values")),
            "localized_description_values": _integer(stats.get("localized_description_values")),
            "base_description_words": _integer(stats.get("base_description_words_total")),
            "localized_description_words": _integer(stats.get("localized_description_words_total")),
            "h3_occupied_cells": _integer(stats.get("h3_occupied_cells")),
            "area_histogram_rows": _integer(stats.get("area_histogram_total_rows")),
        }
    )
    recorder.finish()
    return TrackioReport(
        project=project,
        space_id=space_id,
        run_name=run_name,
        dashboard_url=dashboard_url(project, space_id),
        enabled=recorder.started_successfully,
        point_count=len(points) + 1,
        failure_reason=recorder.failure_reason,
    )


__all__ = [
    "DEFAULT_TRACKIO_PROJECT",
    "DEFAULT_TRACKIO_SPACE_ID",
    "TRACKIO_DASHBOARD_URL",
    "TrackioRecorder",
    "TrackioReport",
    "build_retrospective_points",
    "dashboard_url",
    "publish_retrospective",
    "retrospective_run_name",
]
