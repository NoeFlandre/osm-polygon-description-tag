"""Trackio lifecycle wiring remains post-preflight and failure-isolated."""

from __future__ import annotations

from pathlib import Path

from osm_polygon_description_tag.observability.trackio import TrackioRecorder
from osm_polygon_description_tag.workflow.orchestrator import run_and_publish


class _FakeTracker:
    def __init__(self) -> None:
        self.started = False
        self.finished = False
        self.config: dict[str, object] | None = None
        self.logs: list[dict[str, object]] = []

    def start(self, *, config: dict[str, object]) -> bool:
        self.started = True
        self.config = config
        return True

    def log(self, metrics: dict[str, object]) -> None:
        self.logs.append(metrics)

    def finish(self) -> None:
        self.finished = True


def test_run_and_publish_starts_trackio_only_after_preflight(tmp_path: Path) -> None:
    source_root = tmp_path / "raw"
    data_root = tmp_path / "generated"
    source_root.mkdir()
    data_root.mkdir()
    tracker = _FakeTracker()
    preflight_called = False

    def preflight() -> dict[str, object]:
        nonlocal preflight_called
        preflight_called = True
        assert tracker.started is False
        return {"source_count": 0, "osmium_version": "fake"}

    report = run_and_publish(
        source_root=source_root,
        data_root=data_root,
        confirm_repo="NoeFlandre/osm-polygon-description-tag",
        preflight=preflight,
        upload_runner=lambda _command: "revision",
        verifier=lambda _repo_id, _files: "revision",
        tracker=tracker,  # type: ignore[arg-type]
    )

    assert preflight_called is True
    assert tracker.started is True
    assert tracker.finished is True
    assert tracker.config == {"source_count": 0}
    assert tracker.logs == [
        {
            "step": 1,
            "dataset_rows": 0,
            "dataset_output_bytes": 0,
            "completed_sources": 0,
            "per_pbf_uploads": 0,
        }
    ]
    assert report.source_count == 0


def test_trackio_recorder_is_the_public_live_tracker_type() -> None:
    assert TrackioRecorder.__name__ == "TrackioRecorder"
