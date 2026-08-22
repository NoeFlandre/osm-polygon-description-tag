"""Tests for Trackio metrics without network access."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import ClassVar

import pytest

import osm_polygon_description_tag.observability.trackio as trackio
from osm_polygon_description_tag.observability.trackio import (
    DEFAULT_TRACKIO_PROJECT,
    DEFAULT_TRACKIO_SPACE_ID,
    TrackioRecorder,
    build_dataset_summary,
    build_per_pbf_rows,
    build_snapshot_payload,
    build_snapshot_points,
    dashboard_url,
    snapshot_run_name,
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

    def Table(self, **kwargs: object) -> dict[str, object]:
        return {"type": "table", **kwargs}

    def Markdown(self, text: str) -> dict[str, object]:
        return {"type": "markdown", "text": text}


def test_recorder_logs_metrics_and_finishes_without_global_state(tmp_path: Path) -> None:
    backend = _FakeBackend()
    recorder = TrackioRecorder(
        data_root=tmp_path,
        backend=backend,
        project=DEFAULT_TRACKIO_PROJECT,
        space_id=DEFAULT_TRACKIO_SPACE_ID,
        run_name="snapshot-test",
    )

    assert recorder.start(config={"source_count": 2}) is True
    recorder.log({"step": 1, "rows": 10})
    recorder.finish()

    assert backend.init_calls == [
        {
            "project": DEFAULT_TRACKIO_PROJECT,
            "name": "snapshot-test",
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


def test_publish_snapshot_logs_file_curve_and_summary(tmp_path: Path, monkeypatch) -> None:
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

    from osm_polygon_description_tag.observability.trackio import publish_snapshot

    report = publish_snapshot(tmp_path, backend=backend)

    assert report.enabled is True
    assert report.point_count == 3
    assert backend.finished == 1
    assert set(backend.logs[-1][0]) == {
        "per_pbf_table",
        "dataset_summary",
        "step_definition",
    }
    assert backend.logs[-1][0]["dataset_summary"]["type"] == "table"


def test_publish_snapshot_forwards_exact_configuration_and_report(monkeypatch) -> None:
    class _Recorder:
        instances: ClassVar[list[_Recorder]] = []

        def __init__(self, **kwargs: object) -> None:
            self.kwargs = kwargs
            self.start_calls: list[dict[str, object]] = []
            self.logs: list[dict[str, object]] = []
            self.snapshot_calls: list[tuple[Path, object]] = []
            self.finish_calls = 0
            self.started_successfully = True
            self.failure_reason = "captured failure"
            self.__class__.instances.append(self)

        def start(self, *, config: dict[str, object]) -> bool:
            self.start_calls.append(config)
            return True

        def log(self, metrics: dict[str, object]) -> None:
            self.logs.append(metrics)

        def log_snapshot(self, data_root: Path, *, stats: object) -> None:
            self.snapshot_calls.append((data_root, stats))

        def finish(self) -> None:
            self.finish_calls += 1

    root = Path("/dataset-root")
    backend = object()
    project = "custom-project"
    space_id = "owner/custom_space"
    run_name = "snapshot-explicit"
    stats = {
        "schema_version": 7,
        "files": [
            {"parquet": "b.parquet", "rows": 3, "output_bytes": 30},
            {"parquet": "a.parquet", "rows": 2, "output_bytes": 20},
        ],
    }

    def collect_stats(data_root: Path) -> dict[str, object]:
        assert data_root is root
        return stats

    monkeypatch.setattr(trackio, "collect_stats", collect_stats)
    monkeypatch.setattr(trackio, "TrackioRecorder", _Recorder)

    report = trackio.publish_snapshot(
        root,
        backend=backend,
        project=project,
        space_id=space_id,
        run_name=run_name,
    )

    recorder = _Recorder.instances[-1]
    expected_config = {
        "dataset_repo": "NoeFlandre/osm-polygon-description-tag",
        "dataset_stats_sha256": hashlib.sha256(
            b'{"files":[{"output_bytes":30,"parquet":"b.parquet","rows":3},{"output_bytes":20,"parquet":"a.parquet","rows":2}],"schema_version":7}'
        ).hexdigest(),
        "schema_version": 7,
        "source_count": 2,
        "step_definition": "PBF index sorted by filename; not time",
    }
    expected_points = [
        {"step": 1, "cumulative_rows": 2, "cumulative_output_bytes": 20},
        {"step": 2, "cumulative_rows": 5, "cumulative_output_bytes": 50},
    ]
    assert recorder.kwargs == {
        "data_root": root,
        "backend": backend,
        "project": project,
        "space_id": space_id,
        "run_name": run_name,
    }
    assert recorder.start_calls == [expected_config]
    assert recorder.logs == expected_points
    assert recorder.snapshot_calls == [(root, stats)]
    assert recorder.finish_calls == 1
    assert report == trackio.TrackioSnapshotReport(
        project=project,
        space_id=space_id,
        run_name=run_name,
        dashboard_url=dashboard_url(project, space_id),
        enabled=True,
        point_count=3,
        failure_reason="captured failure",
    )


def test_publish_snapshot_uses_utc_date_when_run_name_is_omitted(monkeypatch) -> None:
    from datetime import datetime as real_datetime

    class _DateTime:
        @staticmethod
        def now(tz: object) -> real_datetime:
            assert tz is trackio.UTC
            return real_datetime(2026, 8, 22)

    class _Recorder:
        run_names: ClassVar[list[str | None]] = []

        def __init__(self, **kwargs: object) -> None:
            self.__class__.run_names.append(kwargs["run_name"])
            self.started_successfully = False
            self.failure_reason = None

        def start(self, *, config: dict[str, object]) -> bool:
            return False

        def log(self, metrics: dict[str, object]) -> None:
            return None

        def log_snapshot(self, data_root: Path, *, stats: object) -> None:
            return None

        def finish(self) -> None:
            return None

    monkeypatch.setattr(trackio, "datetime", _DateTime)
    monkeypatch.setattr(trackio, "collect_stats", lambda _root: {"files": []})
    monkeypatch.setattr(trackio, "TrackioRecorder", _Recorder)
    monkeypatch.setattr(trackio, "snapshot_run_name", lambda value: f"named-{value}")

    report = trackio.publish_snapshot(Path("/dataset-root"))

    assert _Recorder.run_names[-1] == "named-2026-08-22"
    assert report.run_name == "named-2026-08-22"


def test_snapshot_tables_use_explicit_units_and_rates() -> None:
    stats = {
        "rows": 8,
        "unique_osm_objects": 6,
        "regional_overlap_duplicate_rate": 0.4,
        "base_description_values": 5,
        "localized_description_values": 3,
        "base_description_words_total": 10,
        "localized_description_words_total": 4,
        "output_bytes_total": 1024**3,
        "output_files": 1,
        "rejections": {"no_nonempty_description": 4, "invalid_geometry": 2},
        "files": [
            {
                "source_pbf": "alpha.osm.pbf",
                "rows": 8,
                "source_bytes": 1024**3,
                "output_bytes": 2048,
                "emitted_features": 14,
                "rejections": {"no_nonempty_description": 4, "invalid_geometry": 2},
            }
        ],
    }

    rows = build_per_pbf_rows(stats)
    assert rows[0]["source"] == "alpha.osm.pbf"
    assert rows[0]["input_pbf_gib"] == 1.0
    assert rows[0]["output_parquet_mib"] == 2048 / (1024**2)
    assert rows[0]["description_candidate_rate"] == 10 / 14
    assert rows[0]["technical_acceptance_rate"] == 8 / 10
    assert rows[0]["output_bytes_per_row"] == 256
    assert rows[0]["rejections_by_reason"] == '{"invalid_geometry":2,"no_nonempty_description":4}'
    summary = build_dataset_summary(stats)
    assert {row["metric"] for row in summary} == {
        "Total rows",
        "Unique (osm_type, osm_id)",
        "Regional-overlap duplicate rate",
        "Base description values",
        "Localized description values",
        "Total description words",
        "Output size (GiB)",
        "PBFs",
        "Total technical rejections",
    }
    values = {row["metric"]: row["value"] for row in summary}
    assert values == {
        "Total rows": 8,
        "Unique (osm_type, osm_id)": 6,
        "Regional-overlap duplicate rate": 0.4,
        "Base description values": 5,
        "Localized description values": 3,
        "Total description words": 14,
        "Output size (GiB)": 1.0,
        "PBFs": 1,
        "Total technical rejections": 2,
    }


def test_recorder_disables_itself_when_trackio_fails(tmp_path: Path) -> None:
    class _BrokenBackend(_FakeBackend):
        def init(self, **kwargs: object) -> object:
            raise RuntimeError("dashboard unavailable")

    recorder = TrackioRecorder(data_root=tmp_path, backend=_BrokenBackend())

    assert recorder.start(config={}) is False
    recorder.log({"step": 1, "rows": 1})
    recorder.finish()
    assert recorder.enabled is False


def test_snapshot_points_are_ordered_and_cumulative() -> None:
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

    points = build_snapshot_points(stats)

    assert points == [
        {
            "step": 1,
            "cumulative_rows": 10,
            "cumulative_output_bytes": 1000,
        },
        {
            "step": 2,
            "cumulative_rows": 13,
            "cumulative_output_bytes": 1300,
        },
    ]


def test_trackio_urls_and_run_names_are_stable(monkeypatch) -> None:
    assert dashboard_url(DEFAULT_TRACKIO_PROJECT, DEFAULT_TRACKIO_SPACE_ID) == (
        "https://noeflandre-osm-polygon-description-tag-trackio.static.hf.space/"
        "?project=osm-polygon-description-tag&sidebar=hidden"
    )
    assert dashboard_url("project name_é", "Owner_Name/Space_Name") == (
        "https://owner-name-space-name.static.hf.space/"
        "?project=project%20name_%C3%A9&sidebar=hidden"
    )
    assert dashboard_url("project", "owner/parent/space") == (
        "https://owner-parent/space.static.hf.space/?project=project&sidebar=hidden"
    )
    quote_calls: list[tuple[str, str]] = []

    def quote_spy(value: str, *, safe: str) -> str:
        quote_calls.append((value, safe))
        return "encoded-project"

    monkeypatch.setattr(trackio, "quote", quote_spy)
    assert dashboard_url("project/with-slash", "owner/space") == (
        "https://owner-space.static.hf.space/?project=encoded-project&sidebar=hidden"
    )
    assert quote_calls == [("project/with-slash", "-_.~")]
    assert snapshot_run_name("2026-07-31") == "snapshot-2026-07-31"


def test_trackio_url_and_snapshot_name_reject_invalid_values() -> None:
    with pytest.raises(ValueError, match="space_id"):
        dashboard_url(space_id="invalid")
    with pytest.raises(ValueError, match="^snapshot_date must use YYYY-MM-DD$"):
        snapshot_run_name("not-a-date")


def test_trackio_snapshot_helpers_preserve_order_and_hide_internal_fields() -> None:
    stats = {
        "files": [
            {"parquet": "z.parquet", "rows": 2, "_private": "z"},
            {"parquet": "a.parquet", "rows": 1, "_private": "a"},
            "not-a-row",
        ]
    }
    rows = trackio.build_per_pbf_rows(stats)
    assert [row["source"] for row in rows] == ["a.parquet", "z.parquet"]
    assert all("_private" not in row for row in trackio._public_per_pbf_rows(rows))
    assert trackio._ordered_file_entries({"files": []}) == []
    assert trackio._integer("3") == 0
    assert trackio._number("3") == 0.0
    assert trackio._ratio(3, 2) == 1.5
    assert trackio._ratio(3, 0) == 0.0


def test_trackio_helper_defaults_and_rates_are_explicit() -> None:
    assert trackio._ordered_file_entries({"files": 1}) == []
    ordered = trackio._ordered_file_entries(
        {
            "files": [
                {"rows": 1},
                {"parquet": "b.parquet", "rows": 2},
                "not-a-row",
                {"parquet": "a.parquet", "rows": 3},
            ]
        }
    )
    assert [entry.get("parquet", "") for entry in ordered] == [
        "",
        "a.parquet",
        "b.parquet",
    ]
    assert trackio._rejections({}) == {}
    assert trackio._rejections({"rejections": None}) == {}
    assert trackio._rejections({"rejections": {"z": 2, "a": 3}}) == {"a": 3, "z": 2}

    assert trackio._description_candidates(3, {}) == 3
    assert trackio._description_candidates(0, {}) == 0
    assert trackio._description_candidates(10, {"no_nonempty_description": 20}) == 0
    assert (
        trackio._technical_rejections(
            {"no_nonempty_description": 2, "invalid_geometry": 3, "schema": 4}
        )
        == 7
    )
    assert trackio._pbf_rates(8, 14, 10, 1024**3, 2048, 2) == (
        10 / 14,
        8 / 10,
        2048 / 8,
        8.0,
        2 / 10,
    )

    class _GetSpy(dict[str, object]):
        def __init__(self, *args: object, **kwargs: object) -> None:
            super().__init__(*args, **kwargs)
            self.calls: list[tuple[object, object]] = []

        def get(self, key: object, default: object = None) -> object:
            self.calls.append((key, default))
            return super().get(key, default)

    missing_files = _GetSpy()
    assert trackio._ordered_file_entries(missing_files) == []
    assert missing_files.calls == [("files", [])]
    missing_parquet = _GetSpy(rows=1)
    trackio._ordered_file_entries({"files": [missing_parquet]})
    assert missing_parquet.calls == [("parquet", "")]
    missing_rejections = _GetSpy()
    assert trackio._rejections(missing_rejections) == {}
    assert missing_rejections.calls == [("rejections", {})]


def test_trackio_per_pbf_row_is_a_stable_public_and_internal_contract() -> None:
    row = trackio._per_pbf_row(
        {
            "parquet": "fallback.parquet",
            "rows": 5,
            "emitted_features": 8,
            "source_bytes": 1024**3,
            "output_bytes": 2048,
            "rejections": {"no_nonempty_description": 2, "échec": 1},
        }
    )

    assert row == {
        "source": "fallback.parquet",
        "rows": 5,
        "input_pbf_gib": 1.0,
        "output_parquet_mib": 2048 / (1024**2),
        "description_candidate_rate": 6 / 8,
        "technical_acceptance_rate": 5 / 6,
        "output_bytes_per_row": 2048 / 5,
        "rejections_by_reason": '{"no_nonempty_description":2,"échec":1}',
        "_technical_rejections": 1,
        "_rows_per_input_gib": 5.0,
        "_technical_rejection_rate": 1 / 6,
    }
    assert trackio._per_pbf_row({})["source"] == ""
    assert trackio._public_per_pbf_rows([{"public": 1, "_private": 2}]) == [{"public": 1}]


def test_per_pbf_row_serializes_rejection_keys_independently_of_input_order(monkeypatch) -> None:
    monkeypatch.setattr(trackio, "_rejections", lambda _entry: {"z": 1, "a": 2, "é": 3})
    json_calls: list[dict[str, object]] = []
    original_dumps = trackio.json.dumps

    def dumps_spy(value: object, **kwargs: object) -> str:
        json_calls.append(kwargs)
        return original_dumps(value, **kwargs)

    monkeypatch.setattr(trackio.json, "dumps", dumps_spy)
    row = trackio._per_pbf_row({"rows": 1, "emitted_features": 3})
    assert row["rejections_by_reason"] == '{"a":2,"z":1,"é":3}'
    assert json_calls == [{"ensure_ascii": False, "sort_keys": True, "separators": (",", ":")}]


def test_trackio_stats_summary_uses_fallbacks_and_zero_safe_rates() -> None:
    summary = trackio.build_dataset_summary(
        {
            "rows": 10,
            "base_description_words_total": 2,
            "localized_description_words_total": 3,
        }
    )
    values = {row["metric"]: row["value"] for row in summary}
    assert values["Unique (osm_type, osm_id)"] == 10
    assert values["Regional-overlap duplicate rate"] == 0.0
    assert values["Total description words"] == 5

    fallback_summary = trackio.build_dataset_summary({"rows": 10, "unique_osm_objects": 7})
    fallback_values = {row["metric"]: row["value"] for row in fallback_summary}
    assert fallback_values["Regional-overlap duplicate rate"] == 0.3

    zero_summary = trackio.build_dataset_summary({})
    zero_values = {row["metric"]: row["value"] for row in zero_summary}
    assert zero_values["Regional-overlap duplicate rate"] == 0.0
    assert zero_values["Output size (GiB)"] == 0.0


def test_trackio_plot_specs_are_stable_and_ranked_figure_is_bounded() -> None:
    specs = trackio._plot_specs()
    assert specs == (
        (
            "description_candidate_rate_by_region",
            "Description candidate rate by region (top 30)",
            "Candidate rate",
            "description_candidate_rate",
        ),
        (
            "rows_per_input_gib_by_region",
            "Described polygon rows per input GiB (top 30)",
            "Rows per input GiB",
            "_rows_per_input_gib",
        ),
        (
            "technical_rejection_rate_by_region",
            "Technical rejection rate by region (top 30)",
            "Technical rejection rate",
            "_technical_rejection_rate",
        ),
    )
    figure = trackio._ranked_figure(
        [{"source": "b", "value": 1}, {"source": "a", "value": 2}],
        "value",
        "title",
        "xlabel",
    )
    try:
        assert figure.axes[0].get_xlabel() == "xlabel"
        assert figure.axes[0].get_title() == "title"
        assert [tick.get_text() for tick in figure.axes[0].get_yticklabels()] == ["b", "a"]
    finally:
        figure.clf()


def test_ranked_figure_has_deterministic_sorting_size_style_and_top_limit() -> None:
    from matplotlib.colors import to_hex

    rows = [{"source": f"region-{index:02d}", "value": index} for index in range(31)]
    figure = trackio._ranked_figure(rows, "value", "title", "xlabel")
    try:
        axis = figure.axes[0]
        labels = [tick.get_text() for tick in axis.get_yticklabels()]
        assert len(labels) == 30
        assert labels[0] == "region-01"
        assert labels[-1] == "region-30"
        assert "region-00" not in labels
        assert figure.get_size_inches().tolist() == pytest.approx([10.0, 7.2])
        assert figure.dpi == 120
        assert {to_hex(patch.get_facecolor()) for patch in axis.patches} == {"#4a6fa5"}
        assert all(
            line.get_visible() and line.get_alpha() == 0.25 for line in axis.get_xgridlines()
        )
        assert all(not line.get_visible() for line in axis.get_ygridlines())
        assert [patch.get_width() for patch in axis.patches[:2]] == [1.0, 2.0]
    finally:
        figure.clf()

    tie_figure = trackio._ranked_figure(
        [{"source": "z", "value": 2}, {"source": "a", "value": 2}, {"value": 2}],
        "value",
        "title",
        "xlabel",
    )
    try:
        assert [tick.get_text() for tick in tie_figure.axes[0].get_yticklabels()] == [
            "z",
            "a",
            "",
        ]
    finally:
        tie_figure.clf()

    short_figure = trackio._ranked_figure(
        [{"source": "short", "value": 1}], "value", "title", "xlabel"
    )
    try:
        assert short_figure.get_size_inches()[1] == pytest.approx(6.0)
    finally:
        short_figure.clf()


def test_ranked_figure_uses_empty_source_fallback_for_sorting_and_labels() -> None:
    class _Row(dict[str, object]):
        def __init__(self) -> None:
            super().__init__(value=1)
            self.calls: list[tuple[str, object]] = []

        def get(self, key: str, default: object = None) -> object:
            self.calls.append((key, default))
            return super().get(key, default)

    row = _Row()
    figure = trackio._ranked_figure([row], "value", "title", "xlabel")
    try:
        assert [default for key, default in row.calls if key == "source"] == ["", ""]
    finally:
        figure.clf()


def test_stats_hash_and_stats_reader_are_byte_stable(tmp_path: Path, monkeypatch) -> None:
    stats = {"z": "é", "a": 1}
    expected_json = '{"a":1,"z":"é"}'
    assert trackio._stats_sha256(stats) == hashlib.sha256(expected_json.encode("utf-8")).hexdigest()

    path = tmp_path / "stats.json"
    path.write_text(json.dumps(stats), encoding="utf-8")
    hash_json_calls: list[dict[str, object]] = []
    original_dumps = trackio.json.dumps

    def hash_dumps_spy(value: object, **kwargs: object) -> str:
        hash_json_calls.append(kwargs)
        return original_dumps(value, **kwargs)

    monkeypatch.setattr(trackio.json, "dumps", hash_dumps_spy)
    assert trackio._stats_sha256(stats) == hashlib.sha256(expected_json.encode("utf-8")).hexdigest()
    assert hash_json_calls == [{"ensure_ascii": False, "sort_keys": True, "separators": (",", ":")}]

    calls: list[tuple[Path, tuple[object, ...], dict[str, object]]] = []
    original_read_text = Path.read_text

    def read_text_spy(self: Path, *args: object, **kwargs: object) -> str:
        calls.append((self, args, kwargs))
        return original_read_text(self, *args, **kwargs)

    monkeypatch.setattr(Path, "read_text", read_text_spy)
    assert trackio._read_stats(tmp_path) == stats
    assert calls == [(path, (), {"encoding": "utf-8"})]


def test_load_backend_uses_trackio_module_and_isolates_import_errors(monkeypatch) -> None:
    imported: list[str] = []
    backend = object()

    def load(name: str) -> object:
        imported.append(name)
        return backend

    monkeypatch.setattr(trackio.importlib, "import_module", load)
    assert trackio._load_backend() is backend
    assert imported == ["trackio"]

    def unavailable(_name: str) -> object:
        raise OSError("broken native dependency")

    monkeypatch.setattr(trackio.importlib, "import_module", unavailable)
    assert trackio._load_backend() is None


@pytest.mark.parametrize(
    ("value", "disabled"),
    [
        (None, False),
        ("1", False),
        ("yes", False),
        ("0", True),
        ("FALSE", True),
        ("Off", True),
        ("NO", True),
    ],
)
def test_trackio_environment_switch_is_case_insensitive(
    monkeypatch, value: str | None, disabled: bool
) -> None:
    if value is None:
        monkeypatch.delenv("OSM_POLYGON_DESCRIPTION_TAG_TRACKIO", raising=False)
    else:
        monkeypatch.setenv("OSM_POLYGON_DESCRIPTION_TAG_TRACKIO", value)
    assert trackio._trackio_disabled_by_environment() is disabled


def test_snapshot_factories_receive_exact_tables_and_step_definition() -> None:
    class _FactoryBackend:
        def __init__(self) -> None:
            self.table_calls: list[dict[str, object]] = []
            self.markdown_calls: list[str] = []

        def Table(self, **kwargs: object) -> dict[str, object]:
            self.table_calls.append(kwargs)
            return {"kind": "table", **kwargs}

        def Markdown(self, text: str) -> dict[str, object]:
            self.markdown_calls.append(text)
            return {"kind": "markdown", "text": text}

    backend = _FactoryBackend()
    rows = [{"source": "a", "rows": 2, "_internal": 99}]
    stats = {"rows": 2, "output_files": 1}
    payload: dict[str, object] = {}

    trackio._add_snapshot_tables(payload, backend, rows, stats)
    trackio._add_step_definition(payload, backend)

    assert backend.table_calls == [
        {"data": [{"source": "a", "rows": 2}]},
        {"data": trackio.build_dataset_summary(stats)},
    ]
    assert backend.markdown_calls == [
        "Step is the 1-based PBF index after sorting source filenames; it is not time."
    ]
    assert payload["per_pbf_table"] == {"kind": "table", "data": [{"source": "a", "rows": 2}]}
    assert payload["dataset_summary"] == {
        "kind": "table",
        "data": trackio.build_dataset_summary(stats),
    }
    assert payload["step_definition"] == {
        "kind": "markdown",
        "text": backend.markdown_calls[0],
    }
    empty_payload: dict[str, object] = {}
    trackio._add_snapshot_tables(empty_payload, object(), rows, stats)
    trackio._add_step_definition(empty_payload, object())
    trackio._add_snapshot_media(empty_payload, object(), Path("/missing"))
    assert empty_payload == {}


def test_snapshot_media_and_plots_forward_exact_arguments_and_cleanup(
    tmp_path: Path, monkeypatch
) -> None:
    class _MediaPlotBackend:
        def __init__(self) -> None:
            self.image_calls: list[tuple[Path, dict[str, object]]] = []
            self.html_calls: list[tuple[object, dict[str, object]]] = []

        def Image(self, path: Path, **kwargs: object) -> dict[str, object]:
            self.image_calls.append((path, kwargs))
            return {"kind": "image", "path": path, **kwargs}

        def Html(self, figure: object, **kwargs: object) -> dict[str, object]:
            self.html_calls.append((figure, kwargs))
            return {"kind": "html", "figure": figure, **kwargs}

    class _Figure:
        def __init__(self, index: int) -> None:
            self.index = index
            self.clf_calls = 0

        def clf(self) -> None:
            self.clf_calls += 1

    backend = _MediaPlotBackend()
    assets = tmp_path / "assets"
    assets.mkdir()
    density = assets / "description_polygon_density.png"
    density.write_bytes(b"density")
    area = assets / "area_distribution.png"
    area.write_bytes(b"area")
    plot_calls: list[tuple[object, str, str, str]] = []
    figures: list[_Figure] = []

    def make_figure(rows: object, value_key: str, title: str, xlabel: str) -> _Figure:
        plot_calls.append((rows, value_key, title, xlabel))
        figure = _Figure(len(figures))
        figures.append(figure)
        return figure

    monkeypatch.setattr(trackio, "_ranked_figure", make_figure)
    rows = [{"source": "alpha", "description_candidate_rate": 0.5}]
    payload: dict[str, object] = {}
    trackio._add_snapshot_media(payload, backend, tmp_path)
    trackio._add_snapshot_plots(payload, backend, rows)

    assert backend.image_calls == [
        (density, {"caption": "H3 density map"}),
        (area, {"caption": "Area distribution histogram"}),
    ]
    assert [call[1] for call in backend.html_calls] == [
        {"caption": title} for _, title, _, _ in trackio._plot_specs()
    ]
    assert plot_calls == [
        (rows, value_key, title, xlabel) for _, title, xlabel, value_key in trackio._plot_specs()
    ]
    assert [call[0] for call in backend.html_calls] == figures
    assert all(figure.clf_calls == 1 for figure in figures)
    assert set(payload) == {
        "h3_density_map",
        "area_distribution_histogram",
        "description_candidate_rate_by_region",
        "rows_per_input_gib_by_region",
        "technical_rejection_rate_by_region",
    }


def test_snapshot_media_without_an_image_factory_is_a_noop(tmp_path: Path) -> None:
    payload: dict[str, object] = {}
    trackio._add_snapshot_media(payload, object(), tmp_path)
    assert payload == {}


def test_snapshot_plots_skip_missing_backend_and_empty_rows(monkeypatch) -> None:
    calls: list[object] = []
    monkeypatch.setattr(trackio, "_ranked_figure", lambda *args: calls.append(args))
    payload: dict[str, object] = {}
    trackio._add_snapshot_plots(payload, object(), [{"source": "a"}])
    trackio._add_snapshot_plots(payload, _FakeBackend(), [])
    assert payload == {}
    assert calls == []


def test_snapshot_payload_includes_media_and_ranked_plots(tmp_path: Path) -> None:
    class _MediaBackend(_FakeBackend):
        def Image(self, path: Path, **kwargs: object) -> dict[str, object]:
            return {"type": "image", "path": str(path), **kwargs}

        def Html(self, figure: object, **kwargs: object) -> dict[str, object]:
            return {"type": "html", "figure": figure, **kwargs}

    assets = tmp_path / "assets"
    assets.mkdir()
    (assets / "description_polygon_density.png").write_bytes(b"h3")
    (assets / "area_distribution.png").write_bytes(b"area")
    stats = {
        "rows": 8,
        "files": [
            {
                "source_pbf": "alpha.osm.pbf",
                "parquet": "alpha.parquet",
                "rows": 8,
                "source_bytes": 1024,
                "output_bytes": 2048,
                "emitted_features": 10,
                "rejections": {"no_nonempty_description": 1, "invalid_geometry": 1},
            }
        ],
    }

    payload = build_snapshot_payload(_MediaBackend(), tmp_path, stats)

    assert payload["h3_density_map"]["type"] == "image"
    assert payload["area_distribution_histogram"]["type"] == "image"
    assert payload["description_candidate_rate_by_region"]["type"] == "html"
    assert payload["rows_per_input_gib_by_region"]["type"] == "html"
    assert payload["technical_rejection_rate_by_region"]["type"] == "html"


def test_trackio_stats_helpers_reject_non_sequence_files() -> None:
    assert build_per_pbf_rows({"files": "not-a-list"}) == []
    assert build_snapshot_points({"files": "not-a-list"}) == []


def test_load_backend_returns_none_when_import_is_unavailable(monkeypatch) -> None:
    import osm_polygon_description_tag.observability.trackio as trackio

    def unavailable(_name: str) -> object:
        raise ImportError("missing")

    monkeypatch.setattr(trackio.importlib, "import_module", unavailable)
    assert trackio._load_backend() is None
