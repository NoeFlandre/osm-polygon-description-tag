"""Optional Trackio metrics for retrospective and live pipeline runs.

Trackio is deliberately isolated behind :class:`TrackioRecorder`.  A missing
installation, missing Hugging Face credentials, or a dashboard outage disables
metrics and never fails extraction, validation, resumability, or publication.
When enabled, local Trackio data lives below ``<data-root>/logs/trackio`` and
the completed run is synchronized to the public static Space.
"""

from __future__ import annotations

import hashlib
import importlib
import json
import os
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Protocol, cast
from urllib.parse import quote

from osm_polygon_description_tag.dataset.reporting import collect_stats

DEFAULT_TRACKIO_PROJECT = "osm-polygon-description-tag"
DEFAULT_TRACKIO_SPACE_ID = "NoeFlandre/osm-polygon-description-tag-trackio"
TRACKIO_DASHBOARD_URL = (
    "https://noeflandre-osm-polygon-description-tag-trackio.static.hf.space/"
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
    return f"https://{host}.static.hf.space/?project={quote(project, safe='-_.~')}&sidebar=hidden"


def retrospective_run_name(snapshot_date: str) -> str:
    """Return an explicit, human-readable name for a dataset snapshot."""
    try:
        datetime.strptime(snapshot_date, "%Y-%m-%d")
    except ValueError as error:
        raise ValueError("snapshot_date must use YYYY-MM-DD") from error
    return f"snapshot-{snapshot_date}"


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
    points: list[dict[str, object]] = []
    for step, entry in enumerate(ordered, start=1):
        source_rows = _integer(entry.get("rows"))
        source_output_bytes = _integer(entry.get("output_bytes"))
        cumulative_rows += source_rows
        cumulative_output_bytes += source_output_bytes
        points.append(
            {
                "step": step,
                "cumulative_rows": cumulative_rows,
                "cumulative_output_bytes": cumulative_output_bytes,
            }
        )
    return points


def _number(value: object) -> float:
    return float(value) if isinstance(value, int | float) else 0.0


def _rejections(entry: Mapping[str, Any]) -> dict[str, int]:
    raw = entry.get("rejections", {})
    if not isinstance(raw, Mapping):
        return {}
    return {str(key): _integer(value) for key, value in sorted(raw.items())}


def build_per_pbf_rows(stats: Mapping[str, Any]) -> list[dict[str, object]]:
    """Build the factual per-PBF table used by the static dashboard.

    A description candidate is an exported polygon that did not fail the
    non-empty-description filter. Technical acceptance is the fraction of
    candidates that survived all subsequent geometry/schema checks.
    """
    files = stats.get("files", [])
    if not isinstance(files, Sequence) or isinstance(files, str | bytes | bytearray):
        return []
    rows: list[dict[str, object]] = []
    for entry in sorted(
        (item for item in files if isinstance(item, Mapping)),
        key=lambda item: str(item.get("parquet", "")),
    ):
        included = _integer(entry.get("rows"))
        emitted = _integer(entry.get("emitted_features"))
        rejection_counts = _rejections(entry)
        no_description = rejection_counts.get("no_nonempty_description", 0)
        candidates = max(emitted - no_description, 0)
        technical_rejections = sum(
            count
            for reason, count in rejection_counts.items()
            if reason != "no_nonempty_description"
        )
        source_bytes = _integer(entry.get("source_bytes"))
        output_bytes = _integer(entry.get("output_bytes"))
        rows.append(
            {
                "source": str(entry.get("source_pbf", entry.get("parquet", ""))),
                "rows": included,
                "input_pbf_gib": source_bytes / (1024**3),
                "output_parquet_mib": output_bytes / (1024**2),
                "description_candidate_rate": candidates / emitted if emitted else 0.0,
                "technical_acceptance_rate": included / candidates if candidates else 0.0,
                "output_bytes_per_row": output_bytes / included if included else 0.0,
                "rejections_by_reason": json.dumps(
                    rejection_counts, ensure_ascii=False, sort_keys=True, separators=(",", ":")
                ),
                "_technical_rejections": technical_rejections,
                "_rows_per_input_gib": included / (source_bytes / (1024**3))
                if source_bytes
                else 0.0,
                "_technical_rejection_rate": (
                    technical_rejections / candidates if candidates else 0.0
                ),
            }
        )
    return rows


def build_dataset_summary(stats: Mapping[str, Any]) -> list[dict[str, object]]:
    """Build one concise summary table from the validated stats payload."""
    rows = _integer(stats.get("rows"))
    base_values = _integer(stats.get("base_description_values"))
    localized_values = _integer(stats.get("localized_description_values"))
    technical_rejections = sum(
        count for reason, count in _rejections(stats).items() if reason != "no_nonempty_description"
    )
    unique_objects = _integer(stats.get("unique_osm_objects", rows))
    duplicate_rate = _number(
        stats.get(
            "regional_overlap_duplicate_rate",
            (rows - unique_objects) / rows if rows else 0.0,
        )
    )
    return [
        {"metric": "Total rows", "value": rows},
        {"metric": "Unique (osm_type, osm_id)", "value": unique_objects},
        {
            "metric": "Regional-overlap duplicate rate",
            "value": duplicate_rate,
        },
        {"metric": "Base description values", "value": base_values},
        {"metric": "Localized description values", "value": localized_values},
        {
            "metric": "Total description words",
            "value": _integer(stats.get("base_description_words_total"))
            + _integer(stats.get("localized_description_words_total")),
        },
        {
            "metric": "Output size (GiB)",
            "value": _integer(stats.get("output_bytes_total")) / (1024**3),
        },
        {"metric": "PBFs", "value": _integer(stats.get("output_files"))},
        {"metric": "Total technical rejections", "value": technical_rejections},
    ]


def _public_per_pbf_rows(rows: Sequence[Mapping[str, object]]) -> list[dict[str, object]]:
    return [{key: value for key, value in row.items() if not key.startswith("_")} for row in rows]


def _ranked_figure(
    rows: Sequence[Mapping[str, object]],
    value_key: str,
    title: str,
    xlabel: str,
) -> Any:
    import matplotlib

    matplotlib.use("Agg")
    import matplotlib.pyplot as plt

    ranked = sorted(
        rows,
        key=lambda row: (-_number(row.get(value_key)), str(row.get("source", ""))),
    )[:30]
    ranked = list(reversed(ranked))
    figure, axis = plt.subplots(figsize=(10, max(6, len(ranked) * 0.24)), dpi=120)
    labels = [str(row.get("source", "")) for row in ranked]
    values = [_number(row.get(value_key)) for row in ranked]
    axis.barh(labels, values, color="#4a6fa5")
    axis.set_title(title)
    axis.set_xlabel(xlabel)
    axis.grid(axis="x", alpha=0.25)
    figure.tight_layout()
    return figure


def build_snapshot_payload(
    backend: TrackioBackend,
    data_root: Path,
    stats: Mapping[str, Any],
) -> dict[str, object]:
    """Build Trackio tables, media, and ranked plots for one dataset snapshot."""
    per_pbf_rows = build_per_pbf_rows(stats)
    payload: dict[str, object] = {}

    table_factory = getattr(backend, "Table", None)
    if callable(table_factory):
        payload["per_pbf_table"] = table_factory(data=_public_per_pbf_rows(per_pbf_rows))
        payload["dataset_summary"] = table_factory(data=build_dataset_summary(stats))

    markdown_factory = getattr(backend, "Markdown", None)
    if callable(markdown_factory):
        payload["step_definition"] = markdown_factory(
            "Step is the 1-based PBF index after sorting source filenames; it is not time."
        )

    image_factory = getattr(backend, "Image", None)
    if callable(image_factory):
        media = (
            ("h3_density_map", "assets/description_polygon_density.png", "H3 density map"),
            (
                "area_distribution_histogram",
                "assets/area_distribution.png",
                "Area distribution histogram",
            ),
        )
        for key, relative_path, caption in media:
            path = data_root / relative_path
            if path.is_file():
                payload[key] = image_factory(path, caption=caption)

    html_factory = getattr(backend, "Html", None)
    if callable(html_factory) and per_pbf_rows:
        plot_specs = (
            (
                "description_candidate_rate_by_region",
                "Description candidate rate by region (top 30)",
                "Candidate rate",
            ),
            (
                "rows_per_input_gib_by_region",
                "Described polygon rows per input GiB (top 30)",
                "Rows per input GiB",
            ),
            (
                "technical_rejection_rate_by_region",
                "Technical rejection rate by region (top 30)",
                "Technical rejection rate",
            ),
        )
        key_to_value = {
            plot_specs[0][0]: "description_candidate_rate",
            plot_specs[1][0]: "_rows_per_input_gib",
            plot_specs[2][0]: "_technical_rejection_rate",
        }
        for key, title, xlabel in plot_specs:
            figure = _ranked_figure(per_pbf_rows, key_to_value[key], title, xlabel)
            try:
                payload[key] = html_factory(figure, caption=title)
            finally:
                figure.clf()

    return payload


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
        try:
            trackio_dir = self.data_root / "logs" / "trackio"
            trackio_dir.mkdir(parents=True, exist_ok=True)
            os.environ["TRACKIO_DIR"] = str(trackio_dir)
            backend = self.backend or _load_backend()
            if backend is None:
                self.failure_reason = "trackio is not installed"
                return False
            # The public dashboard is a static Space, which is free and
            # serves a durable snapshot. Runs log locally during execution;
            # ``finish`` synchronizes that local database to the static Space.
            kwargs: dict[str, object] = {
                "project": self.project,
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

    def log_snapshot(
        self,
        data_root: Path,
        *,
        stats: Mapping[str, Any] | None = None,
    ) -> None:
        """Log the summary tables, media, and ranked plots for a completed run."""
        if not self.enabled or self.backend is None:
            return
        try:
            snapshot_stats = stats if stats is not None else _read_stats(data_root)
            self.log(build_snapshot_payload(self.backend, data_root, snapshot_stats))
        except Exception as error:  # pragma: no cover - defensive integration boundary
            self.failure_reason = f"trackio snapshot failed: {error}"
            self.enabled = False

    def finish(self) -> None:
        """Flush and close the run without propagating Trackio failures."""
        if not self._started or self.backend is None:
            return
        try:
            self.backend.finish()
            sync = getattr(self.backend, "sync", None)
            if callable(sync):
                owner, _, name = self.space_id.partition("/")
                sync(
                    project=self.project,
                    space_id=self.space_id,
                    bucket_id=f"{owner}/{name}-bucket",
                    sdk="static",
                    force=True,
                )
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


def _read_stats(data_root: Path) -> dict[str, Any]:
    path = data_root / "stats.json"
    return cast(dict[str, Any], json.loads(path.read_text(encoding="utf-8")))


def _stats_sha256(stats: Mapping[str, Any]) -> str:
    encoded = json.dumps(stats, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


def publish_retrospective(
    data_root: Path,
    *,
    backend: TrackioBackend | None = None,
    project: str = DEFAULT_TRACKIO_PROJECT,
    space_id: str = DEFAULT_TRACKIO_SPACE_ID,
    run_name: str | None = None,
) -> TrackioReport:
    """Log one completed dataset snapshot and its per-file metric curve."""
    stats = collect_stats(data_root)
    stats_sha256 = _stats_sha256(stats)
    resolved_run_name = run_name or retrospective_run_name(datetime.now(UTC).date().isoformat())
    recorder = TrackioRecorder(
        data_root=data_root,
        backend=backend,
        project=project,
        space_id=space_id,
        run_name=resolved_run_name,
    )
    points = build_retrospective_points(stats)
    config = {
        "dataset_repo": "NoeFlandre/osm-polygon-description-tag",
        "dataset_stats_sha256": stats_sha256,
        "schema_version": _integer(stats.get("schema_version")),
        "source_count": len(points),
        "step_definition": "PBF index sorted by filename; not time",
    }
    recorder.start(config=config)
    for point in points:
        recorder.log(point)
    recorder.log_snapshot(data_root, stats=stats)
    recorder.finish()
    return TrackioReport(
        project=project,
        space_id=space_id,
        run_name=resolved_run_name,
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
    "build_dataset_summary",
    "build_per_pbf_rows",
    "build_retrospective_points",
    "build_snapshot_payload",
    "dashboard_url",
    "publish_retrospective",
    "retrospective_run_name",
]
