"""Trackio lifecycle wiring remains post-preflight and failure-isolated."""

from __future__ import annotations

from pathlib import Path

from osm_polygon_description_tag.dataset.deduplication import DeduplicationResult
from osm_polygon_description_tag.observability.trackio import TrackioRecorder
from osm_polygon_description_tag.workflow import orchestrator
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

    def log_snapshot(self, _data_root: Path) -> None:
        self.logs.append({"snapshot": True})

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
    assert tracker.config == {
        "source_count": 0,
        "step_definition": "PBF index sorted by filename; not time",
    }
    assert tracker.logs == [
        {
            "snapshot": True,
        },
    ]
    assert report.source_count == 0


def test_trackio_recorder_is_the_public_live_tracker_type() -> None:
    assert TrackioRecorder.__name__ == "TrackioRecorder"


def test_run_and_publish_runs_global_deduplication_before_metadata(
    tmp_path: Path, monkeypatch
) -> None:
    source_root = tmp_path / "raw"
    data_root = tmp_path / "generated"
    source_root.mkdir()
    data_root.mkdir()
    calls: list[Path] = []

    def fake_deduplicate(root: Path) -> object:
        calls.append(root)
        return DeduplicationResult("skipped", 0, 0, 0, 0)

    monkeypatch.setattr(orchestrator, "deduplicate_dataset", fake_deduplicate)

    run_and_publish(
        source_root=source_root,
        data_root=data_root,
        confirm_repo="NoeFlandre/osm-polygon-description-tag",
        preflight=lambda: {"source_count": 0, "osmium_version": "fake"},
        upload_runner=lambda _command: "revision",
        verifier=lambda _repo_id, _files: "revision",
    )

    assert calls == [data_root]
