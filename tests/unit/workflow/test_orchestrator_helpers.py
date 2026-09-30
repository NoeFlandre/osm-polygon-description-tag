"""Direct behavioral tests for the orchestration stage boundaries."""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest

from osm_polygon_description_tag.osm.discovery import Source
from osm_polygon_description_tag.runtime.config import Paths
from osm_polygon_description_tag.workflow import orchestrator
from osm_polygon_description_tag.workflow.source_runner import (
    STATUS_BUILT,
    STATUS_REUSED,
    OrchestratorError,
    SourceOutcome,
)
from tests.helpers.orchestration import RecordingLogger as _Logger
from tests.helpers.orchestration import RecordingTracker as _Tracker
from tests.helpers.orchestration import source_runner_workspace as _workspace


def test_run_preflight_uses_injected_check_and_logs_complete_report(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    paths, _source = _workspace(tmp_path)
    logger = _Logger()
    report = {
        "osmium_executable": "osmium-custom",
        "osmium_version": "1.2.3",
        "hub_repo_sha": "remote-sha",
        "source_count": 4,
    }
    calls: list[str] = []

    def preflight() -> dict[str, object]:
        calls.append("injected")
        return report

    def unexpected_default(*_args: object, **_kwargs: object) -> dict[str, object]:
        pytest.fail("injected preflight must bypass the default check")

    monkeypatch.setattr(orchestrator, "default_preflight", unexpected_default)

    assert (
        orchestrator._run_preflight(
            paths,
            preflight=preflight,
            confirm_repo="owner/dataset",
            osmium_executable="osmium",
            logger=logger,
        )
        is report
    )
    assert calls == ["injected"]
    assert logger.preflight_approved is True
    assert logger.preflight_denied is False
    assert logger.events == [
        (
            "preflight",
            {
                "level": "INFO",
                "osmium_executable": "osmium-custom",
                "osmium_version": "1.2.3",
                "hub_repo_sha": "remote-sha",
                "source_count": 4,
            },
        )
    ]


def test_run_preflight_calls_default_check_with_fixed_hf_executable(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    paths, _source = _workspace(tmp_path)
    logger = _Logger()
    calls: list[tuple[object, dict[str, object]]] = []
    report: dict[str, object] = {}

    def default_check(path: object, **kwargs: object) -> dict[str, object]:
        calls.append((path, kwargs))
        return report

    monkeypatch.setattr(orchestrator, "default_preflight", default_check)

    assert (
        orchestrator._run_preflight(
            paths,
            preflight=None,
            confirm_repo="owner/dataset",
            osmium_executable="osmium-custom",
            logger=logger,
        )
        is report
    )
    assert calls == [
        (
            paths,
            {
                "confirm_repo": "owner/dataset",
                "osmium_executable": "osmium-custom",
                "hf_executable": "hf",
            },
        )
    ]
    assert logger.preflight_approved is True
    assert logger.events == [
        (
            "preflight",
            {
                "level": "INFO",
                "osmium_executable": "osmium",
                "osmium_version": "",
                "hub_repo_sha": "",
                "source_count": 0,
            },
        )
    ]


def test_run_preflight_denies_and_reraises_check_failure(
    tmp_path: Path,
) -> None:
    paths, _source = _workspace(tmp_path)
    logger = _Logger()

    def fail() -> dict[str, object]:
        raise RuntimeError("preflight unavailable")

    with pytest.raises(RuntimeError, match="preflight unavailable"):
        orchestrator._run_preflight(
            paths,
            preflight=fail,
            confirm_repo="owner/dataset",
            osmium_executable="osmium",
            logger=logger,
        )

    assert logger.preflight_approved is False
    assert logger.preflight_denied is True
    assert logger.events == [
        (
            "preflight_denied",
            {"level": "ERROR", "reason": "preflight unavailable"},
        )
    ]


def test_discover_run_cleans_owned_temps_discovers_sources_and_starts_tracker(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    paths, source = _workspace(tmp_path)
    logger = _Logger()
    tracker = _Tracker()
    preflight_report = {"hub_repo_sha": "remote-sha"}
    calls: dict[str, object] = {}

    def cleanup(data_root: Path) -> list[Path]:
        calls["cleanup_root"] = data_root
        return [data_root / "stale-a", data_root / "stale-b"]

    def discover(source_root: Path) -> list[Source]:
        calls["source_root"] = source_root
        return [source, source]

    monkeypatch.setattr(orchestrator, "cleanup_stale_owned_temps", cleanup)
    monkeypatch.setattr(orchestrator, "discover_sources", discover)

    sources, report = orchestrator._discover_run(
        paths,
        preflight_report,
        logger=logger,
        tracker=tracker,
    )

    assert sources == [source, source]
    assert report.source_count == 2
    assert report.preflight is preflight_report
    assert calls == {
        "cleanup_root": paths.data_root,
        "source_root": paths.source_root,
    }
    assert logger.events == [
        ("stale_temp_cleanup", {"level": "INFO", "rows": 2}),
        ("sources_discovered", {"level": "INFO", "total": 2}),
    ]
    assert tracker.starts == [
        {
            "source_count": 2,
            "step_definition": "PBF index sorted by filename; not time",
        }
    ]


def test_publish_sources_forwards_each_source_and_tracks_cumulative_progress(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    paths, source = _workspace(tmp_path)
    second_path = paths.source_root / "second.osm.pbf"
    second_path.write_bytes(b"second-source")
    second_output = paths.data_root / "data" / "second.parquet"
    second_output.write_bytes(b"second-parquet")
    second_source = Source(
        path=second_path,
        name=second_path.name,
        output_name=second_output.name,
        size_bytes=second_path.stat().st_size,
        mtime_ns=second_path.stat().st_mtime_ns,
    )
    sources = [source, second_source]
    first_outcome = SourceOutcome(source.name, STATUS_BUILT)
    first_outcome.included_rows = 2
    first_outcome.output_bytes = 11
    second_outcome = SourceOutcome(second_source.name, STATUS_REUSED)
    second_outcome.included_rows = 3
    second_outcome.output_bytes = 13
    outcomes = {source.name: first_outcome, second_source.name: second_outcome}
    report = orchestrator.OrchestrationReport(source_count=2, preflight={})
    logger = _Logger()
    tracker = _Tracker()
    verifier = object()
    upload_runner = object()
    subprocess_runner = object()

    def clock() -> str:
        return "now"

    calls: list[tuple[tuple[object, ...], dict[str, object]]] = []

    def publish_one(*args: object, **kwargs: object) -> tuple[SourceOutcome, bool]:
        calls.append((args, kwargs))
        passed_source = args[1]
        assert isinstance(passed_source, Source)
        return outcomes[passed_source.name], passed_source is source

    monkeypatch.setattr(orchestrator, "_publish_source_if_needed", publish_one)

    orchestrator._publish_sources(
        paths,
        sources,
        outcomes,
        report,
        verifier=verifier,
        upload_timeout=12.0,
        upload_runner=upload_runner,
        clock=clock,
        logger=logger,
        tracker=tracker,
        subprocess_runner=subprocess_runner,
    )

    assert report.outcomes == [first_outcome, second_outcome]
    assert calls == [
        (
            (paths, source, first_outcome),
            {
                "verifier": verifier,
                "upload_timeout": 12.0,
                "upload_runner": upload_runner,
                "clock": clock,
                "logger": logger,
                "source_index": 1,
                "source_total": 2,
                "subprocess_runner": subprocess_runner,
            },
        ),
        (
            (paths, second_source, second_outcome),
            {
                "verifier": verifier,
                "upload_timeout": 12.0,
                "upload_runner": upload_runner,
                "clock": clock,
                "logger": logger,
                "source_index": 2,
                "source_total": 2,
                "subprocess_runner": subprocess_runner,
            },
        ),
    ]
    assert tracker.logs == [
        {"step": 1, "cumulative_rows": 2, "cumulative_output_bytes": 11},
        {"step": 2, "cumulative_rows": 5, "cumulative_output_bytes": 24},
    ]


def test_reconcile_remote_skips_verifiers_without_reconcile_hook(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    paths, _source = _workspace(tmp_path)
    logger = _Logger()

    monkeypatch.setattr(
        orchestrator,
        "create_upload_plan",
        lambda _root: pytest.fail("non-reconciling verifier must not build a plan"),
    )

    orchestrator._reconcile_remote(paths, object(), logger)

    assert logger.events == []


def test_reconcile_remote_calls_hook_with_managed_paths_and_logs_revision(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    paths, _source = _workspace(tmp_path)
    logger = _Logger()
    plan = SimpleNamespace(
        files=[
            SimpleNamespace(relative_path="data/a.parquet"),
            SimpleNamespace(relative_path="README.md"),
        ]
    )
    captured: dict[str, object] = {}

    def create_plan(data_root: Path) -> SimpleNamespace:
        captured["plan_root"] = data_root
        return plan

    class _Verifier:
        def reconcile_managed_files(self, repo_id: str, managed_paths: set[str]) -> str:
            captured["reconcile_args"] = (repo_id, managed_paths)
            return "revision-123"

    monkeypatch.setattr(orchestrator, "create_upload_plan", create_plan)

    orchestrator._reconcile_remote(paths, _Verifier(), logger)

    assert captured == {
        "plan_root": paths.data_root,
        "reconcile_args": (
            "NoeFlandre/osm-polygon-description-tag",
            {"data/a.parquet", "README.md"},
        ),
    }
    assert logger.events == [
        ("remote_reconciliation_start", {"level": "INFO"}),
        (
            "remote_reconciliation_complete",
            {"level": "INFO", "verified_revision": "revision-123"},
        ),
    ]


def test_finalize_local_dataset_validates_deduplicates_and_refreshes_docs(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    paths, source = _workspace(tmp_path)
    sources = [source]
    logger = _Logger()

    def clock() -> str:
        return "now"

    calls: list[tuple[str, object, object]] = []

    def verify(data_paths: Paths, passed_sources: list[Source]) -> None:
        calls.append(("verify", data_paths, passed_sources))

    def deduplicate(data_root: Path) -> SimpleNamespace:
        calls.append(("deduplicate", data_root, None))
        return SimpleNamespace(
            input_rows=10,
            output_rows=7,
            duplicate_rows=3,
            files_changed=2,
            status="changed",
        )

    def refresh(data_paths: Paths, *, clock: object, logger: object) -> None:
        calls.append(("refresh", data_paths, (clock, logger)))

    monkeypatch.setattr(orchestrator, "_verify_final_completeness", verify)
    monkeypatch.setattr(orchestrator, "deduplicate_dataset", deduplicate)
    monkeypatch.setattr(orchestrator, "_refresh_dataset_docs_for_metadata", refresh)

    orchestrator._finalize_local_dataset(paths, sources, clock=clock, logger=logger)

    assert calls == [
        ("verify", paths, sources),
        ("deduplicate", paths.data_root, None),
        ("verify", paths, sources),
        ("refresh", paths, (clock, logger)),
    ]
    assert logger.events == [
        (
            "deduplication_complete",
            {
                "level": "INFO",
                "input_rows": 10,
                "output_rows": 7,
                "duplicate_rows": 3,
                "files_changed": 2,
                "status": "changed",
            },
        )
    ]


def test_build_sources_processes_all_sources_with_index_and_shared_options(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    paths, source = _workspace(tmp_path)
    second_source = Source(
        path=source.path,
        name="second.osm.pbf",
        output_name="second.parquet",
        size_bytes=source.size_bytes,
        mtime_ns=source.mtime_ns,
    )
    sources = [source, second_source]
    logger = _Logger()
    exporter = object()

    def clock() -> str:
        return "now"

    calls: list[tuple[tuple[object, ...], dict[str, object]]] = []
    first_outcome = SourceOutcome(source.name, STATUS_BUILT)
    second_outcome = SourceOutcome(second_source.name, STATUS_REUSED)

    def process_one(*args: object, **kwargs: object) -> SourceOutcome:
        calls.append((args, kwargs))
        passed_source = args[0]
        assert isinstance(passed_source, Source)
        return first_outcome if passed_source is source else second_outcome

    monkeypatch.setattr(orchestrator, "_process_one", process_one)

    outcomes = orchestrator._build_sources(
        sources,
        paths,
        clock=clock,
        exporter=exporter,
        progress_interval=37,
        logger=logger,
        osmium_executable="osmium-custom",
    )

    assert outcomes == {
        source.name: first_outcome,
        second_source.name: second_outcome,
    }
    assert calls == [
        (
            (source, paths),
            {
                "clock": clock,
                "exporter": exporter,
                "progress_interval": 37,
                "logger": logger,
                "source_index": 1,
                "source_total": 2,
                "osmium_executable": "osmium-custom",
            },
        ),
        (
            (second_source, paths),
            {
                "clock": clock,
                "exporter": exporter,
                "progress_interval": 37,
                "logger": logger,
                "source_index": 2,
                "source_total": 2,
                "osmium_executable": "osmium-custom",
            },
        ),
    ]


def test_resolve_paths_prefers_explicit_paths_and_requires_both_roots(
    tmp_path: Path,
) -> None:
    explicit = Paths(source_root=tmp_path / "explicit-raw", data_root=tmp_path / "explicit-data")
    source_root = tmp_path / "raw"
    data_root = tmp_path / "data"

    assert orchestrator._resolve_paths(explicit, source_root, data_root) is explicit

    resolved = orchestrator._resolve_paths(None, source_root, data_root)
    assert resolved.source_root == source_root
    assert resolved.data_root == data_root

    for missing_source, missing_data in (
        (None, data_root),
        (source_root, None),
        (None, None),
    ):
        with pytest.raises(
            OrchestratorError,
            match=r"^paths or \(source_root, data_root\) is required$",
        ):
            orchestrator._resolve_paths(None, missing_source, missing_data)


def test_default_verifier_uses_dataset_local_hub_cache(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    paths, _source = _workspace(tmp_path)
    verifier = object()
    captured: list[dict[str, object]] = []

    def factory(**kwargs: object) -> object:
        captured.append(kwargs)
        return verifier

    monkeypatch.setattr(orchestrator, "default_hub_verifier_factory", factory)

    assert orchestrator._default_verifier(paths) is verifier
    assert captured == [{"cache_dir": paths.data_root / ".cache" / "huggingface" / "hub"}]


def test_default_verifier_falls_back_only_for_unsupported_cache_keyword(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    paths, _source = _workspace(tmp_path)
    verifier = object()
    calls: list[dict[str, object]] = []

    def unsupported_keyword(**kwargs: object) -> object:
        calls.append(kwargs)
        if kwargs:
            raise TypeError("unexpected keyword argument 'cache_dir'")
        return verifier

    monkeypatch.setattr(orchestrator, "default_hub_verifier_factory", unsupported_keyword)

    assert orchestrator._default_verifier(paths) is verifier
    assert calls == [
        {"cache_dir": paths.data_root / ".cache" / "huggingface" / "hub"},
        {},
    ]


def test_default_verifier_reraises_other_factory_type_errors(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    paths, _source = _workspace(tmp_path)

    def fail(**_kwargs: object) -> object:
        raise TypeError("factory is broken")

    monkeypatch.setattr(orchestrator, "default_hub_verifier_factory", fail)

    with pytest.raises(TypeError, match="^factory is broken$"):
        orchestrator._default_verifier(paths)


def test_verification_logging_helpers_emit_stable_events_and_allow_no_logger(
    tmp_path: Path,
) -> None:
    _paths, source = _workspace(tmp_path)
    logger = _Logger()

    orchestrator._log_verification_start(logger, source)
    orchestrator._log_verification_complete(logger, source, "revision-123")
    orchestrator._log_verification_start(None, source)
    orchestrator._log_verification_complete(None, source, "revision-123")

    assert logger.events == [
        ("upload_complete", {"level": "INFO", "source": source.name}),
        ("verification_start", {"level": "INFO", "source": source.name}),
        (
            "verification_complete",
            {
                "level": "INFO",
                "source": source.name,
                "verified_revision": "revision-123",
            },
        ),
    ]


def test_verify_source_plan_forwards_plan_and_source_and_logs_verified_revision(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    _paths, source = _workspace(tmp_path)
    plan = object()
    verifier = object()
    logger = _Logger()
    captured: dict[str, object] = {}

    def call_verifier(passed_plan: object, passed_source: Source, passed_verifier: object) -> str:
        captured["args"] = (passed_plan, passed_source, passed_verifier)
        return "verified-revision"

    monkeypatch.setattr(orchestrator, "_call_source_verifier", call_verifier)

    assert (
        orchestrator._verify_source_plan(
            plan,
            source,
            verifier=verifier,
            logger=logger,
        )
        == "verified-revision"
    )
    assert captured["args"] == (plan, source, verifier)
    assert logger.events == [
        ("upload_complete", {"level": "INFO", "source": source.name}),
        ("verification_start", {"level": "INFO", "source": source.name}),
        (
            "verification_complete",
            {
                "level": "INFO",
                "source": source.name,
                "verified_revision": "verified-revision",
            },
        ),
    ]


def test_verify_source_plan_refuses_to_record_without_a_verifier(
    tmp_path: Path,
) -> None:
    _paths, source = _workspace(tmp_path)

    with pytest.raises(
        OrchestratorError,
        match=r"^no Hub verifier supplied; refusing to record an unknown revision$",
    ):
        orchestrator._verify_source_plan(object(), source, verifier=None, logger=_Logger())
