"""Tests for Trackio metrics without network access."""

from __future__ import annotations

from pathlib import Path

from osm_polygon_description_tag.observability.trackio import (
    DEFAULT_TRACKIO_PROJECT,
    DEFAULT_TRACKIO_SPACE_ID,
    TrackioRecorder,
    build_retrospective_points,
    dashboard_url,
    retrospective_run_name,
)


class _FakeBackend:
    def __init__(self) -> None:
        self.init_calls: list[dict[str, object]] = []
        self.logs: list[tuple[dict[str, object], int | None]] = []
        self.finished = 0

    def init(self, **kwargs: object) -> object:
        self.init_calls.append(kwargs)
        return object()

    def log(self, metrics: dict[str, object], step: int | None = None) -> None:
        self.logs.append((metrics, step))

    def finish(self) -> None:
        self.finished += 1


def test_recorder_logs_metrics_and_finishes_without_global_state(tmp_path: Path) -> None:
    backend = _FakeBackend()
    recorder = TrackioRecorder(
        data_root=tmp_path,
        backend=backend,
        project=DEFAULT_TRACKIO_PROJECT,
        space_id=DEFAULT_TRACKIO_SPACE_ID,
        run_name="retrospective-test",
    )

    assert recorder.start(config={"source_count": 2}) is True
    recorder.log({"step": 1, "rows": 10})
    recorder.finish()

    assert backend.init_calls == [
        {
            "project": DEFAULT_TRACKIO_PROJECT,
            "name": "retrospective-test",
            "config": {"source_count": 2},
        }
    ]
    assert backend.logs == [({"rows": 10}, 1)]
    assert backend.finished == 1


def test_recorder_syncs_local_run_to_static_space(tmp_path: Path) -> None:
    class _SyncBackend(_FakeBackend):
        def __init__(self) -> None:
            super().__init__()
            self.sync_calls: list[dict[str, object]] = []

        def sync(self, **kwargs: object) -> str:
            self.sync_calls.append(kwargs)
            return "NoeFlandre/osm-polygon-description-tag-trackio"

    backend = _SyncBackend()
    recorder = TrackioRecorder(data_root=tmp_path, backend=backend)
    assert recorder.start(config={}) is True
    recorder.finish()

    assert backend.sync_calls == [
        {
            "project": DEFAULT_TRACKIO_PROJECT,
            "space_id": DEFAULT_TRACKIO_SPACE_ID,
            "bucket_id": "NoeFlandre/osm-polygon-description-tag-trackio-bucket",
            "sdk": "static",
            "force": True,
        }
    ]


def test_recorder_sets_trackio_dir_before_loading_backend(tmp_path: Path, monkeypatch) -> None:
    observed: list[str | None] = []
    backend = _FakeBackend()

    def load_backend() -> _FakeBackend:
        import os

        observed.append(os.environ.get("TRACKIO_DIR"))
        return backend

    monkeypatch.delenv("TRACKIO_DIR", raising=False)
    monkeypatch.setenv("OSM_POLYGON_DESCRIPTION_TAG_TRACKIO", "1")
    monkeypatch.setattr(
        "osm_polygon_description_tag.observability.trackio._load_backend", load_backend
    )

    recorder = TrackioRecorder(data_root=tmp_path)
    assert recorder.start(config={}) is True
    assert observed == [str(tmp_path / "logs" / "trackio")]


def test_publish_retrospective_logs_file_curve_and_summary(tmp_path: Path, monkeypatch) -> None:
    backend = _FakeBackend()
    stats = {
        "schema_version": 2,
        "rows": 13,
        "output_bytes_total": 1300,
        "source_bytes_total": 2600,
        "base_description_values": 5,
        "localized_description_values": 8,
        "base_description_words_total": 9,
        "localized_description_words_total": 14,
        "h3_occupied_cells": 3,
        "area_histogram_total_rows": 13,
        "files": [
            {"parquet": "a.parquet", "rows": 10, "output_bytes": 1000, "source_bytes": 2000},
            {"parquet": "b.parquet", "rows": 3, "output_bytes": 300, "source_bytes": 600},
        ],
    }
    monkeypatch.setattr(
        "osm_polygon_description_tag.observability.trackio.collect_stats",
        lambda _root: stats,
    )

    from osm_polygon_description_tag.observability.trackio import publish_retrospective

    report = publish_retrospective(tmp_path, backend=backend)

    assert report.enabled is True
    assert report.point_count == 3
    assert backend.finished == 1
    assert backend.logs[-1][0]["dataset_rows"] == 13


def test_recorder_disables_itself_when_trackio_fails(tmp_path: Path) -> None:
    class _BrokenBackend(_FakeBackend):
        def init(self, **kwargs: object) -> object:
            raise RuntimeError("dashboard unavailable")

    recorder = TrackioRecorder(data_root=tmp_path, backend=_BrokenBackend())

    assert recorder.start(config={}) is False
    recorder.log({"step": 1, "rows": 1})
    recorder.finish()
    assert recorder.enabled is False


def test_retrospective_points_are_ordered_and_cumulative() -> None:
    stats = {
        "rows": 13,
        "output_bytes_total": 1300,
        "source_bytes_total": 2600,
        "files": [
            {
                "parquet": "a.parquet",
                "rows": 10,
                "output_bytes": 1000,
                "source_bytes": 2000,
            },
            {
                "parquet": "b.parquet",
                "rows": 3,
                "output_bytes": 300,
                "source_bytes": 600,
            },
        ],
    }

    points = build_retrospective_points(stats)

    assert points == [
        {
            "step": 1,
            "source_rows": 10,
            "cumulative_rows": 10,
            "source_output_bytes": 1000,
            "cumulative_output_bytes": 1000,
            "source_bytes": 2000,
            "cumulative_source_bytes": 2000,
        },
        {
            "step": 2,
            "source_rows": 3,
            "cumulative_rows": 13,
            "source_output_bytes": 300,
            "cumulative_output_bytes": 1300,
            "source_bytes": 600,
            "cumulative_source_bytes": 2600,
        },
    ]


def test_trackio_urls_and_run_names_are_stable() -> None:
    assert dashboard_url(DEFAULT_TRACKIO_PROJECT, DEFAULT_TRACKIO_SPACE_ID) == (
        "https://noeflandre-osm-polygon-description-tag-trackio.static.hf.space/"
        "?project=osm-polygon-description-tag&sidebar=hidden"
    )
    assert retrospective_run_name("a" * 64) == "retrospective-aaaaaaaaaaaa"
