"""Direct behavioral tests for the orchestration stage boundaries."""

from __future__ import annotations

import inspect
import subprocess
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import Mock

import pytest

import osm_polygon_description_tag.workflow.orchestrator as orchestrator
from osm_polygon_description_tag.config import Paths
from osm_polygon_description_tag.osm.discovery import Source
from osm_polygon_description_tag.workflow.source_runner import (
    STATUS_BUILT,
    STATUS_PUBLISHED,
    STATUS_REUSED,
    OrchestratorError,
    SourceOutcome,
)


class _Logger:
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


class _Tracker:
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


def _workspace(tmp_path: Path) -> tuple[Paths, Source]:
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


def _manifest() -> SimpleNamespace:
    return SimpleNamespace(counts=SimpleNamespace(included_rows=7))


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


def test_publish_source_marks_matching_state_without_upload(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    paths, source = _workspace(tmp_path)
    logger = _Logger()
    outcome = SourceOutcome(source.name, STATUS_BUILT)
    observed: dict[str, object] = {}
    manifest = _manifest()

    def read_manifest(path: Path) -> SimpleNamespace:
        observed["manifest_path"] = path
        return manifest

    def state_read(data_root: Path) -> dict[str, object]:
        observed["state_root"] = data_root
        return {}

    def matches(
        existing: object,
        passed_manifest: object,
        passed_source: Source,
        output_path: Path,
    ) -> bool:
        observed["match_args"] = (existing, passed_manifest, passed_source, output_path)
        return True

    monkeypatch.setattr(orchestrator, "read_manifest", read_manifest)
    monkeypatch.setattr(orchestrator, "_state_read_publication_state", state_read)
    monkeypatch.setattr(orchestrator, "_published_state_matches", matches)
    monkeypatch.setattr(
        orchestrator,
        "_execute_publication",
        lambda *args, **kwargs: pytest.fail("matching publication state must not upload"),
    )

    returned, uploaded = orchestrator._publish_source_if_needed(
        paths,
        source,
        outcome,
        verifier=lambda *_args: "unused",
        upload_timeout=12.0,
        upload_runner=lambda _command: "unused",
        clock=lambda: "now",
        logger=logger,
        source_index=1,
        source_total=1,
    )

    assert returned is outcome
    assert uploaded is False
    assert outcome.status == STATUS_PUBLISHED
    assert outcome.included_rows == 7
    assert outcome.output_bytes == len(b"parquet")
    assert outcome.note == "already published; nothing to do"
    assert logger.events == []
    assert observed["state_root"] == paths.data_root
    assert observed["manifest_path"] == paths.data_root / "manifests" / "region.manifest.json"
    match_args = observed["match_args"]
    assert isinstance(match_args, tuple)
    assert match_args == ({}, manifest, source, paths.data_root / "data" / "region.parquet")


def test_publish_source_uses_canonical_output_and_manifest_boundaries(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    _paths, source = _workspace(tmp_path)

    class _Node:
        def __init__(self, *parts: str) -> None:
            self.parts = parts

        def __truediv__(self, part: str) -> _Node:
            return _Node(*self.parts, part)

        def stat(self) -> SimpleNamespace:
            return SimpleNamespace(st_size=23)

    root = _Node("data-root")
    paths = SimpleNamespace(data_root=root)
    logger = _Logger()
    outcome = SourceOutcome(source.name, STATUS_BUILT)
    captured: dict[str, object] = {}

    def read_manifest(path: _Node) -> SimpleNamespace:
        captured["manifest_path"] = path
        return _manifest()

    def matches(
        existing: object, manifest: object, passed_source: Source, output_path: _Node
    ) -> bool:
        captured["match_args"] = (existing, manifest, passed_source, output_path)
        return True

    monkeypatch.setattr(orchestrator, "read_manifest", read_manifest)
    monkeypatch.setattr(
        orchestrator,
        "_state_read_publication_state",
        lambda _root: {"published": {source.name: {"state": "current"}}},
    )
    monkeypatch.setattr(orchestrator, "_published_state_matches", matches)
    monkeypatch.setattr(
        orchestrator,
        "_execute_publication",
        lambda *args, **kwargs: pytest.fail("matching state must not upload"),
    )

    returned, uploaded = orchestrator._publish_source_if_needed(
        paths,
        source,
        outcome,
        verifier=None,
        upload_timeout=None,
        upload_runner=None,
        clock=lambda: "now",
        logger=logger,
        source_index=1,
        source_total=1,
    )

    assert returned is outcome
    assert uploaded is False
    assert captured["manifest_path"].parts == (
        "data-root",
        "manifests",
        "region.manifest.json",
    )
    match_args = captured["match_args"]
    assert isinstance(match_args, tuple)
    assert match_args[0] == {"state": "current"}
    assert match_args[2] is source
    assert match_args[3].parts == ("data-root", "data", "region.parquet")


def test_publish_source_uploads_stale_state_and_persists_verified_identity(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    paths, source = _workspace(tmp_path)
    logger = _Logger()
    outcome = SourceOutcome(source.name, STATUS_PUBLISHED)
    captured: dict[str, object] = {}
    manifest = _manifest()

    def verifier(*_args):
        return "unused"

    def upload_runner(_command):
        return "unused"

    monkeypatch.setattr(orchestrator, "read_manifest", lambda _path: manifest)
    monkeypatch.setattr(
        orchestrator, "_state_read_publication_state", lambda _root: {"published": {}}
    )
    monkeypatch.setattr(orchestrator, "_published_state_matches", lambda *args: False)

    def execute(*args: object, **kwargs: object) -> str:
        captured["execute"] = (args, kwargs)
        return "verified-revision"

    def build_plan(*args: object) -> SimpleNamespace:
        captured.setdefault("plan_args", []).append(args)  # type: ignore[union-attr]
        return SimpleNamespace(identity_sha256="plan-identity")

    def source_identity(path: Path) -> SimpleNamespace:
        captured["source_path"] = path
        return SimpleNamespace(sha256="source-identity")

    def output_identity(path: Path) -> SimpleNamespace:
        captured["output_path"] = path
        return SimpleNamespace(sha256="output-identity")

    monkeypatch.setattr(orchestrator, "_execute_publication", execute)
    monkeypatch.setattr(orchestrator, "_build_per_pbf_upload_plan", build_plan)
    monkeypatch.setattr(orchestrator, "source_identity_for", source_identity)
    monkeypatch.setattr(orchestrator, "output_identity_for", output_identity)

    def write_state(*args: object, **kwargs: object) -> None:
        captured["args"] = args
        captured["kwargs"] = kwargs

    monkeypatch.setattr(orchestrator, "_write_publication_state", write_state)

    returned, uploaded = orchestrator._publish_source_if_needed(
        paths,
        source,
        outcome,
        verifier=verifier,
        upload_timeout=12.0,
        upload_runner=upload_runner,
        clock=lambda: "finished-at",
        logger=logger,
        source_index=2,
        source_total=3,
    )

    assert returned is outcome
    assert uploaded is True
    assert outcome.status == STATUS_REUSED
    assert outcome.remote_revision == "verified-revision"
    assert outcome.note == "published after verified upload"
    assert captured["args"] == (paths.data_root,)
    assert captured["kwargs"] == {
        "source_name": source.name,
        "source_sha256": "source-identity",
        "output_sha256": "output-identity",
        "output_bytes": len(b"parquet"),
        "remote_revision": "verified-revision",
        "artifact_identity": "plan-identity",
        "completed_at": "finished-at",
    }
    execute_args = captured["execute"]
    assert isinstance(execute_args, tuple)
    assert execute_args[0] == (paths, source)
    assert execute_args[1]["verifier"] is verifier
    assert execute_args[1]["timeout"] == 12.0
    assert execute_args[1]["upload_runner"] is upload_runner
    assert execute_args[1]["logger"] is logger
    assert captured["plan_args"] == [(paths.data_root, source.name)] * 1
    assert captured["source_path"] == source.path
    assert captured["output_path"] == paths.data_root / "data" / "region.parquet"
    assert logger.events == [
        (
            "upload_start",
            {
                "level": "INFO",
                "source": source.name,
                "source_index": 2,
                "source_total": 3,
            },
        ),
        (
            "state_written",
            {
                "level": "INFO",
                "source": source.name,
                "source_index": 2,
                "source_total": 3,
            },
        ),
    ]


def test_publish_source_marks_failed_upload_and_preserves_error(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    paths, source = _workspace(tmp_path)
    logger = _Logger()
    outcome = SourceOutcome(source.name, STATUS_BUILT)
    monkeypatch.setattr(orchestrator, "read_manifest", lambda _path: _manifest())
    monkeypatch.setattr(
        orchestrator, "_state_read_publication_state", lambda _root: {"published": {}}
    )
    monkeypatch.setattr(orchestrator, "_published_state_matches", lambda *args: False)
    monkeypatch.setattr(
        orchestrator,
        "_execute_publication",
        lambda *args, **kwargs: (_ for _ in ()).throw(OrchestratorError("upload failed")),
    )

    with pytest.raises(OrchestratorError, match="upload failed"):
        orchestrator._publish_source_if_needed(
            paths,
            source,
            outcome,
            verifier=lambda *_args: "unused",
            upload_timeout=None,
            upload_runner=None,
            clock=lambda: "now",
            logger=logger,
            source_index=1,
            source_total=1,
        )

    assert outcome.status == orchestrator.STATUS_FAILED
    assert outcome.note == "upload failed"
    assert logger.events == [
        (
            "upload_start",
            {
                "level": "INFO",
                "source": source.name,
                "source_index": 1,
                "source_total": 1,
            },
        ),
        (
            "upload_failed",
            {
                "level": "ERROR",
                "source": source.name,
                "source_index": 1,
                "source_total": 1,
                "reason": "upload failed",
            },
        ),
    ]


def test_publish_source_retains_deduplicated_note_when_state_write_fails(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    paths, source = _workspace(tmp_path)
    logger = _Logger()
    outcome = SourceOutcome(source.name, STATUS_PUBLISHED)
    monkeypatch.setattr(orchestrator, "read_manifest", lambda _path: _manifest())
    monkeypatch.setattr(
        orchestrator, "_state_read_publication_state", lambda _root: {"published": {}}
    )
    monkeypatch.setattr(orchestrator, "_published_state_matches", lambda *args: False)
    monkeypatch.setattr(
        orchestrator, "_execute_publication", lambda *args, **kwargs: "verified-revision"
    )
    monkeypatch.setattr(
        orchestrator,
        "_build_per_pbf_upload_plan",
        lambda *_args: SimpleNamespace(identity_sha256="plan-identity"),
    )
    monkeypatch.setattr(
        orchestrator,
        "source_identity_for",
        lambda _path: SimpleNamespace(sha256="source-identity"),
    )
    monkeypatch.setattr(
        orchestrator,
        "output_identity_for",
        lambda _path: SimpleNamespace(sha256="output-identity"),
    )
    monkeypatch.setattr(
        orchestrator,
        "_write_publication_state",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(RuntimeError("state unavailable")),
    )

    with pytest.raises(RuntimeError, match="state unavailable"):
        orchestrator._publish_source_if_needed(
            paths,
            source,
            outcome,
            verifier=None,
            upload_timeout=None,
            upload_runner=None,
            clock=lambda: "now",
            logger=logger,
            source_index=1,
            source_total=1,
        )

    assert outcome.note == "deduplicated artifact requires upload"


def test_run_and_publish_executes_stages_in_order_and_finishes_tracker(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    paths, source = _workspace(tmp_path)
    source_root = tmp_path / "provided-source-root"
    data_root = tmp_path / "provided-data-root"
    logger = _Logger()
    tracker = _Tracker()
    report = orchestrator.OrchestrationReport(source_count=1, preflight={})
    outcome = SourceOutcome(source.name, STATUS_BUILT)
    stages: list[str] = []
    captured: dict[str, object] = {}

    def preflight() -> dict[str, object]:
        return {"supplied": True}

    def upload_runner(_command: list[str]) -> str:
        return "unused"

    def verifier(_repo_id: str, _files: Any) -> str:
        return "unused"

    def verifier_factory() -> Any:
        return verifier

    def exporter(*_args: object, **_kwargs: object) -> list[Any]:
        return []

    def clock() -> str:
        return "now"

    def resolve_paths(*args: object) -> Paths:
        captured["resolve_paths"] = args
        return paths

    def run_preflight(*args: object, **kwargs: object) -> dict[str, object]:
        stages.append("preflight")
        captured["run_preflight"] = (args, kwargs)
        return {"source_count": 1}

    def discover_run(*args: object, **kwargs: object) -> tuple[list[Source], object]:
        stages.append("discover")
        captured["discover_run"] = (args, kwargs)
        report.preflight = {"source_count": 1}
        return [source], report

    def resolve_verifier(*args: object, **kwargs: object) -> object:
        stages.append("verifier")
        captured["resolve_verifier"] = (args, kwargs)
        return verifier

    def build_sources(*args: object, **kwargs: object) -> dict[str, SourceOutcome]:
        stages.append("build")
        captured["build_sources"] = (args, kwargs)
        return {source.name: outcome}

    def finalize_local_dataset(*args: object, **kwargs: object) -> None:
        stages.append("finalize")
        captured["finalize_local_dataset"] = (args, kwargs)

    def publish_sources(*args: object, **kwargs: object) -> None:
        stages.append("publish")
        captured["publish_sources"] = (args, kwargs)
        report.outcomes.append(outcome)

    def reconcile_remote(*args: object, **kwargs: object) -> None:
        stages.append("reconcile")
        captured["reconcile_remote"] = (args, kwargs)

    def publish_final_metadata(*args: object, **kwargs: object) -> str:
        stages.append("metadata")
        captured["publish_final_metadata"] = (args, kwargs)
        return "final-revision"

    monkeypatch.setattr(orchestrator, "_resolve_paths", resolve_paths)
    monkeypatch.setattr(orchestrator, "_run_preflight", run_preflight)
    monkeypatch.setattr(orchestrator, "_discover_run", discover_run)
    monkeypatch.setattr(orchestrator, "_resolve_verifier", resolve_verifier)
    monkeypatch.setattr(orchestrator, "_build_sources", build_sources)
    monkeypatch.setattr(orchestrator, "_finalize_local_dataset", finalize_local_dataset)
    monkeypatch.setattr(orchestrator, "_publish_sources", publish_sources)
    monkeypatch.setattr(orchestrator, "_reconcile_remote", reconcile_remote)
    monkeypatch.setattr(orchestrator, "_publish_final_metadata", publish_final_metadata)

    returned = orchestrator._run_and_publish(
        source_root=source_root,
        data_root=data_root,
        confirm_repo="owner/dataset",
        preflight=preflight,
        upload_runner=upload_runner,
        clock=clock,
        paths=paths,
        exporter=exporter,
        verifier=verifier,
        verifier_factory=verifier_factory,
        upload_timeout=5.0,
        progress_interval=37,
        logger=logger,
        tracker=tracker,
        osmium_executable="osmium",
    )

    assert returned is report
    assert stages == [
        "preflight",
        "discover",
        "verifier",
        "build",
        "finalize",
        "publish",
        "reconcile",
        "metadata",
    ]
    assert report.preflight == {"source_count": 1}
    assert report.final_remote_revision == "final-revision"
    assert report.outcomes == [outcome]
    assert tracker.snapshots == [paths.data_root]
    assert logger.flushed is True
    assert captured["resolve_paths"] == (paths, source_root, data_root)
    assert captured["run_preflight"] == (
        (paths,),
        {
            "preflight": preflight,
            "confirm_repo": "owner/dataset",
            "osmium_executable": "osmium",
            "logger": logger,
        },
    )
    assert captured["discover_run"] == (
        (paths, {"source_count": 1}),
        {"logger": logger, "tracker": tracker},
    )
    assert captured["resolve_verifier"] == (
        (paths,),
        {
            "verifier": verifier,
            "verifier_factory": verifier_factory,
            "upload_runner": upload_runner,
        },
    )
    assert captured["build_sources"] == (
        ([source], paths),
        {
            "clock": clock,
            "exporter": exporter,
            "progress_interval": 37,
            "logger": logger,
            "osmium_executable": "osmium",
        },
    )
    assert captured["finalize_local_dataset"] == (
        (paths, [source]),
        {"clock": clock, "logger": logger},
    )
    assert captured["publish_sources"] == (
        (
            paths,
            [source],
            {source.name: outcome},
            report,
        ),
        {
            "verifier": verifier,
            "upload_timeout": 5.0,
            "upload_runner": upload_runner,
            "clock": clock,
            "logger": logger,
            "tracker": tracker,
        },
    )
    assert captured["reconcile_remote"] == ((paths, verifier, logger), {})
    assert captured["publish_final_metadata"] == (
        (paths,),
        {
            "verifier": verifier,
            "upload_runner": upload_runner,
            "upload_timeout": 5.0,
            "clock": clock,
            "logger": logger,
        },
    )
    assert logger.events == [
        (
            "run_summary",
            {
                "level": "INFO",
                "result": "completed",
                "source_count": 1,
                "per_pbf_uploads": 1,
            },
        )
    ]


def test_publication_state_wrappers_preserve_success_and_translate_failures(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    state = {"published": {"region.osm.pbf": {"remote_revision": "r1"}}}
    assert_state_calls: list[Path] = []

    def read_state(data_root: Path) -> dict[str, object]:
        assert_state_calls.append(data_root)
        return state

    monkeypatch.setattr(orchestrator, "_state_read_publication_state", read_state)
    assert orchestrator.read_publication_state(tmp_path) is state
    assert assert_state_calls == [tmp_path]

    write_calls: list[tuple[tuple[object, ...], dict[str, object]]] = []

    def write_state(*args: object, **kwargs: object) -> dict[str, object]:
        write_calls.append((args, kwargs))
        return {"written": True}

    monkeypatch.setattr(orchestrator, "_state_write_publication_state", write_state)
    assert orchestrator._write_publication_state("root", source_name="region") == {"written": True}
    assert write_calls == [(("root",), {"source_name": "region"})]

    read_error = orchestrator.PublicationStateError("read is broken")

    def fail_read(_data_root: Path) -> dict[str, object]:
        raise read_error

    monkeypatch.setattr(orchestrator, "_state_read_publication_state", fail_read)
    with pytest.raises(OrchestratorError, match=r"^read is broken$") as read_info:
        orchestrator.read_publication_state(tmp_path)
    assert read_info.value.__cause__ is read_error

    write_error = orchestrator.PublicationStateError("write is broken")

    def fail_write(*_args: object, **_kwargs: object) -> dict[str, object]:
        raise write_error

    monkeypatch.setattr(orchestrator, "_state_write_publication_state", fail_write)
    with pytest.raises(OrchestratorError, match=r"^write is broken$") as write_info:
        orchestrator._write_publication_state("root")
    assert write_info.value.__cause__ is write_error


def test_clock_helpers_preserve_injected_clock_and_produce_utc_isoformat() -> None:
    def injected_clock() -> str:
        return "injected"

    assert orchestrator._resolve_clock(injected_clock) is injected_clock
    assert orchestrator._resolve_clock(None) is orchestrator._default_clock

    generated = orchestrator._default_clock()
    parsed = datetime.fromisoformat(generated)
    assert parsed.tzinfo is not None
    assert parsed.utcoffset() is not None


def test_ensure_logger_reuses_injected_logger_and_reports_missing_root(
    tmp_path: Path,
) -> None:
    logger = object()

    def clock():
        return "now"

    assert orchestrator._ensure_logger(
        logger,
        paths=None,
        data_root=None,
        clock=clock,
    ) == (logger, False)

    with pytest.raises(
        OrchestratorError,
        match=r"^logger requires paths or data_root$",
    ):
        orchestrator._ensure_logger(None, paths=None, data_root=None, clock=clock)


def test_ensure_logger_constructs_owned_logger_from_explicit_paths(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    paths, _source = _workspace(tmp_path)

    def clock():
        return "now"

    owned_logger = object()
    captured: dict[str, object] = {}

    def make_logger(**kwargs: object) -> object:
        captured.update(kwargs)
        return owned_logger

    monkeypatch.setattr(orchestrator, "RunLogger", make_logger)
    monkeypatch.setattr(orchestrator.uuid, "uuid4", lambda: "run-uuid")

    result = orchestrator._ensure_logger(
        None,
        paths=paths,
        data_root=tmp_path / "ignored-data-root",
        clock=clock,
    )

    assert result == (owned_logger, True)
    assert captured == {
        "data_root": paths.data_root,
        "run_id": "run-uuid",
        "clock": clock,
        "buffer_preflight": True,
    }


def test_resolve_verifier_observes_precedence_and_upload_only_mode(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    paths, _source = _workspace(tmp_path)
    explicit = object()
    factory_result = object()
    default_result = object()
    factory_calls: list[str] = []
    default_calls: list[Paths] = []

    def factory() -> object:
        factory_calls.append("factory")
        return factory_result

    def default(paths_arg: Paths) -> object:
        default_calls.append(paths_arg)
        return default_result

    monkeypatch.setattr(orchestrator, "_default_verifier", default)

    assert (
        orchestrator._resolve_verifier(
            paths,
            verifier=explicit,
            verifier_factory=factory,
            upload_runner=object(),
        )
        is explicit
    )
    assert factory_calls == []
    assert (
        orchestrator._resolve_verifier(
            paths,
            verifier=None,
            verifier_factory=factory,
            upload_runner=None,
        )
        is factory_result
    )
    assert (
        orchestrator._resolve_verifier(
            paths,
            verifier=None,
            verifier_factory=None,
            upload_runner=object(),
        )
        is None
    )
    assert (
        orchestrator._resolve_verifier(
            paths,
            verifier=None,
            verifier_factory=None,
            upload_runner=None,
        )
        is default_result
    )
    assert default_calls == [paths]


def test_default_upload_forwards_identity_timeout_and_retry_callback(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    plan = SimpleNamespace(identity_sha256="plan-identity")
    logger = _Logger()
    calls: list[tuple[tuple[object, ...], dict[str, object]]] = []

    def execute_upload(*args: object, **kwargs: object) -> None:
        calls.append((args, kwargs))

    monkeypatch.setattr(orchestrator, "execute_upload", execute_upload)
    orchestrator._run_default_source_upload(plan, timeout=12.5, logger=logger)

    assert len(calls) == 1
    args, kwargs = calls[0]
    assert args == (plan,)
    assert kwargs["confirmation"] == "plan-identity"
    assert kwargs["timeout"] == 12.5
    retry_observer = kwargs["retry_observer"]
    assert callable(retry_observer)
    retry_observer(attempt=2, reason="timeout")  # type: ignore[operator]
    assert logger.events == [("upload_retry", {"attempt": 2, "reason": "timeout"})]
    assert orchestrator._source_retry_observer(None) is None


def test_injected_source_upload_forwards_canonical_command_and_rejects_empty_revision(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    paths, source = _workspace(tmp_path)
    commands: list[list[str]] = []
    expected_command = ["canonical", "upload"]

    monkeypatch.setattr(
        orchestrator,
        "per_pbf_command",
        lambda data_root, source_name: (
            expected_command
            if data_root == paths.data_root and source_name == source.name
            else pytest.fail("unexpected planner arguments")
        ),
    )

    def runner(command: list[str]) -> str:
        commands.append(command)
        return "revision-1"

    assert orchestrator._run_injected_source_upload(paths, source, runner) is None
    assert commands == [expected_command]

    with pytest.raises(
        orchestrator.PublicationError,
        match=r"^upload runner returned empty revision$",
    ):
        orchestrator._run_injected_source_upload(paths, source, lambda _command: "")


def test_call_source_verifier_forwards_repo_and_files_and_wraps_failures(
    tmp_path: Path,
) -> None:
    _paths, source = _workspace(tmp_path)
    plan = SimpleNamespace(files=("data/a.parquet", "README.md"))
    captured: list[tuple[object, ...]] = []

    def verifier(repo_id: str, files: object) -> str:
        captured.append((repo_id, files))
        return "revision-1"

    assert orchestrator._call_source_verifier(plan, source, verifier) == "revision-1"
    assert captured == [(orchestrator.REPO_ID, plan.files)]

    with pytest.raises(
        OrchestratorError,
        match=(
            rf"^Hub verifier returned no revision for {source.name}; "
            r"refusing to record 'unknown'$"
        ),
    ):
        orchestrator._call_source_verifier(plan, source, lambda *_args: "")

    with pytest.raises(
        OrchestratorError,
        match=rf"^Hub verifier failed for {source.name}: verifier broke$",
    ):
        orchestrator._call_source_verifier(
            plan,
            source,
            lambda *_args: (_ for _ in ()).throw(ValueError("verifier broke")),
        )


def test_upload_source_plan_dispatches_branches_and_wraps_upload_errors(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    paths, source = _workspace(tmp_path)
    plan = object()
    default_upload = Mock()
    injected_upload = Mock()
    logger = _Logger()
    monkeypatch.setattr(orchestrator, "_run_default_source_upload", default_upload)
    monkeypatch.setattr(orchestrator, "_run_injected_source_upload", injected_upload)

    orchestrator._upload_source_plan(
        plan,
        paths,
        source,
        timeout=7.0,
        upload_runner=None,
        logger=logger,
    )
    default_upload.assert_called_once_with(plan, timeout=7.0, logger=logger)
    injected_upload.assert_not_called()

    def runner(_command):
        return "revision"

    orchestrator._upload_source_plan(
        plan,
        paths,
        source,
        timeout=None,
        upload_runner=runner,
        logger=None,
    )
    injected_upload.assert_called_once_with(paths, source, runner)

    for error in (
        orchestrator.PublicationError("publication broke"),
        subprocess.CalledProcessError(1, ["upload"]),
        subprocess.TimeoutExpired(["upload"], timeout=1.0),
    ):
        failing = Mock(side_effect=error)
        monkeypatch.setattr(orchestrator, "_run_injected_source_upload", failing)
        with pytest.raises(
            OrchestratorError,
            match=rf"^upload failed for {source.name}:",
        ):
            orchestrator._upload_source_plan(
                plan,
                paths,
                source,
                timeout=None,
                upload_runner=runner,
                logger=None,
            )


def test_execute_publication_builds_validates_uploads_and_verifies_in_order(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    paths, source = _workspace(tmp_path)
    plan = object()
    verifier = object()
    logger = _Logger()
    calls: list[tuple[str, object]] = []

    def build_plan(data_root: Path, source_name: str) -> object:
        calls.append(("build", (data_root, source_name)))
        return plan

    def validate_plan(data_root: Path) -> object:
        calls.append(("validate", data_root))
        return object()

    def upload_plan(*args: object, **kwargs: object) -> None:
        calls.append(("upload", (args, kwargs)))

    def verify_plan(*args: object, **kwargs: object) -> str:
        calls.append(("verify", (args, kwargs)))
        return "verified-revision"

    monkeypatch.setattr(orchestrator, "_build_per_pbf_upload_plan", build_plan)
    monkeypatch.setattr(orchestrator, "create_upload_plan", validate_plan)
    monkeypatch.setattr(orchestrator, "_upload_source_plan", upload_plan)
    monkeypatch.setattr(orchestrator, "_verify_source_plan", verify_plan)

    result = orchestrator._execute_publication(
        paths,
        source,
        verifier=verifier,
        timeout=9.0,
        upload_runner=None,
        logger=logger,
    )

    assert result == "verified-revision"
    assert calls == [
        ("build", (paths.data_root, source.name)),
        ("validate", paths.data_root),
        (
            "upload",
            (
                (plan, paths, source),
                {
                    "timeout": 9.0,
                    "upload_runner": None,
                    "logger": logger,
                },
            ),
        ),
        (
            "verify",
            (
                (plan, source),
                {"verifier": verifier, "logger": logger},
            ),
        ),
    ]


def test_orchestrator_compatibility_wrappers_forward_all_metadata_arguments(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    paths, source = _workspace(tmp_path)

    def clock():
        return "now"

    logger = _Logger()
    verifier = object()

    def upload_runner(_command):
        return "revision"

    captured: dict[str, object] = {}

    def refresh(*args: object, **kwargs: object) -> None:
        captured["refresh"] = (args, kwargs)

    def verify(*args: object, **kwargs: object) -> None:
        captured["verify"] = (args, kwargs)

    monkeypatch.setattr(orchestrator.finalization, "refresh_dataset_docs", refresh)
    monkeypatch.setattr(orchestrator.finalization, "verify_final_completeness", verify)
    orchestrator._refresh_dataset_docs_for_metadata(paths, clock=clock, logger=logger)
    orchestrator._verify_final_completeness(paths, [source])

    assert captured["refresh"] == (
        (paths,),
        {
            "clock": clock,
            "logger": logger,
            "docs_generator": orchestrator.generate_dataset_docs,
        },
    )
    assert captured["verify"] == ((paths, [source]), {})

    upload_metadata = Mock(return_value="metadata-revision")
    monkeypatch.setattr(orchestrator, "_upload_final_metadata", upload_metadata)
    assert (
        orchestrator._publish_final_metadata(
            paths,
            verifier=verifier,
            upload_runner=upload_runner,
            upload_timeout=4.0,
            clock=clock,
            logger=logger,
        )
        == "metadata-revision"
    )
    upload_metadata.assert_called_once_with(
        paths,
        verifier=verifier,
        upload_runner=upload_runner,
        upload_timeout=4.0,
        clock=clock,
        logger=logger,
    )


def test_upload_final_metadata_uses_default_clock_and_canonical_validator(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    paths, _source = _workspace(tmp_path)
    verifier = object()

    def upload_runner(_command):
        return "revision"

    logger = _Logger()

    def explicit_clock():
        return "explicit"

    calls: list[tuple[tuple[object, ...], dict[str, object]]] = []

    def upload(*args: object, **kwargs: object) -> str:
        calls.append((args, kwargs))
        return "metadata-revision"

    monkeypatch.setattr(orchestrator.finalization, "upload_final_metadata", upload)

    assert (
        orchestrator._upload_final_metadata(
            paths,
            verifier=verifier,
            upload_runner=upload_runner,
            upload_timeout=3.0,
            clock=None,
            logger=logger,
        )
        == "metadata-revision"
    )
    assert calls[0] == (
        (paths,),
        {
            "verifier": verifier,
            "upload_runner": upload_runner,
            "upload_timeout": 3.0,
            "clock": orchestrator._default_clock,
            "logger": logger,
            "plan_validator": orchestrator.create_upload_plan,
        },
    )

    orchestrator._upload_final_metadata(
        paths,
        verifier=verifier,
        upload_runner=upload_runner,
        upload_timeout=None,
        clock=explicit_clock,
        logger=None,
    )
    assert calls[1][1]["clock"] is explicit_clock
    assert calls[1][1]["logger"] is None


def test_reconcile_remote_logs_empty_revision_as_empty_string(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    paths, _source = _workspace(tmp_path)
    logger = _Logger()
    plan = SimpleNamespace(files=[])

    class _Verifier:
        def reconcile_managed_files(self, _repo_id: str, _paths: set[str]) -> None:
            return None

    monkeypatch.setattr(orchestrator, "create_upload_plan", lambda _root: plan)
    orchestrator._reconcile_remote(paths, _Verifier(), logger)

    assert logger.events == [
        ("remote_reconciliation_start", {"level": "INFO"}),
        (
            "remote_reconciliation_complete",
            {"level": "INFO", "verified_revision": ""},
        ),
    ]


def test_optional_subprocess_bridge_selects_exact_execution_path(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    report = object()
    direct_calls: list[dict[str, object]] = []
    bridge_calls: list[tuple[object, dict[str, object]]] = []

    def direct(**kwargs: object) -> object:
        direct_calls.append(kwargs)
        return report

    def bridged(runner: object, **kwargs: object) -> object:
        bridge_calls.append((runner, kwargs))
        return report

    monkeypatch.setattr(orchestrator, "_run_and_publish", direct)
    monkeypatch.setattr(orchestrator, "_run_with_subprocess_bridge", bridged)
    assert orchestrator._run_with_optional_subprocess_bridge(None, option=1) is report
    runner = object()
    assert orchestrator._run_with_optional_subprocess_bridge(runner, option=2) is report
    assert direct_calls == [{"option": 1}]
    assert bridge_calls == [(runner, {"option": 2})]


def test_subprocess_bridge_forwards_command_restores_runner_and_keeps_signature(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import osm_polygon_description_tag.publication.upload as publication_upload

    original_runner = publication_upload._default_runner_with_retry
    subprocess_calls: list[list[str]] = []
    captured: dict[str, object] = {}
    report = object()

    def subprocess_runner(command: list[str]) -> None:
        subprocess_calls.append(command)

    def run_and_publish(**kwargs: object) -> object:
        bridge = publication_upload._default_runner_with_retry
        parameters = inspect.signature(bridge).parameters
        captured["defaults"] = {
            name: parameters[name].default
            for name in (
                "max_retries",
                "backoff_seconds",
                "backoff_factor",
                "backoff_cap_seconds",
                "timeout",
                "_runner",
                "retry_observer",
            )
        }
        bridge(
            ["osmium", "export"],
            max_retries=9,
            backoff_seconds=4.0,
            backoff_factor=3.0,
            backoff_cap_seconds=11.0,
            timeout=8.0,
            _runner=lambda *_args: None,
            retry_observer=lambda **_fields: None,
        )
        captured["kwargs"] = kwargs
        return report

    monkeypatch.setattr(orchestrator, "_run_and_publish", run_and_publish)
    result = orchestrator._run_with_subprocess_bridge(
        subprocess_runner,
        option="value",
    )

    assert result is report
    assert subprocess_calls == [["osmium", "export"]]
    assert captured["kwargs"] == {"option": "value"}
    assert captured["defaults"] == {
        "max_retries": 3,
        "backoff_seconds": 2.0,
        "backoff_factor": 2.0,
        "backoff_cap_seconds": 60.0,
        "timeout": None,
        "_runner": None,
        "retry_observer": None,
    }
    assert publication_upload._default_runner_with_retry is original_runner


def test_run_and_publish_forwards_all_options_and_closes_owned_resources(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    paths, source = _workspace(tmp_path)
    provided_logger = object()
    owned_logger = Mock()
    tracker = Mock()

    def resolved_clock():
        return "resolved"

    captured: dict[str, object] = {}
    report = object()

    def clock():
        return "input"

    def preflight():
        return {"ready": True}

    def upload_runner(_command):
        return "revision"

    def exporter(*_args, **_kwargs):
        return []

    verifier = object()

    def verifier_factory():
        return verifier

    def subprocess_runner(_command):
        return None

    def resolve_clock(actual: object) -> object:
        captured["resolve_clock"] = actual
        return resolved_clock

    def ensure_logger(actual: object, **kwargs: object) -> tuple[object, bool]:
        captured["ensure_logger"] = (actual, kwargs)
        return owned_logger, True

    def run_optional(actual_runner: object, **kwargs: object) -> object:
        captured["run_optional"] = (actual_runner, kwargs)
        return report

    monkeypatch.setattr(orchestrator, "_resolve_clock", resolve_clock)
    monkeypatch.setattr(orchestrator, "_ensure_logger", ensure_logger)
    monkeypatch.setattr(orchestrator, "_run_with_optional_subprocess_bridge", run_optional)

    returned = orchestrator.run_and_publish(
        source_root=source.path.parent,
        data_root=paths.data_root,
        confirm_repo="owner/dataset",
        preflight=preflight,
        upload_runner=upload_runner,
        clock=clock,
        paths=paths,
        exporter=exporter,
        verifier=verifier,
        verifier_factory=verifier_factory,
        upload_timeout=12.0,
        subprocess_runner=subprocess_runner,
        progress_interval=37,
        logger=provided_logger,
        tracker=tracker,
        osmium_executable="osmium-custom",
    )

    assert returned is report
    assert captured["resolve_clock"] is clock
    assert captured["ensure_logger"] == (
        provided_logger,
        {"paths": paths, "data_root": paths.data_root, "clock": resolved_clock},
    )
    actual_runner, kwargs = captured["run_optional"]
    assert actual_runner is subprocess_runner
    assert kwargs == {
        "source_root": source.path.parent,
        "data_root": paths.data_root,
        "confirm_repo": "owner/dataset",
        "preflight": preflight,
        "upload_runner": upload_runner,
        "clock": resolved_clock,
        "paths": paths,
        "exporter": exporter,
        "verifier": verifier,
        "verifier_factory": verifier_factory,
        "upload_timeout": 12.0,
        "progress_interval": 37,
        "logger": owned_logger,
        "tracker": tracker,
        "osmium_executable": "osmium-custom",
    }
    owned_logger.close.assert_called_once_with()
    tracker.finish.assert_called_once_with()
    assert (
        inspect.signature(orchestrator.run_and_publish).parameters["progress_interval"].default
        == 100_000
    )


def test_run_and_publish_logs_interrupt_and_finishes_tracker(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    logger = Mock()
    tracker = Mock()

    def resolved_clock():
        return "resolved"

    monkeypatch.setattr(orchestrator, "_resolve_clock", lambda _clock: resolved_clock)
    monkeypatch.setattr(
        orchestrator,
        "_ensure_logger",
        lambda _logger, **_kwargs: (logger, False),
    )

    def interrupted(*_args: object, **_kwargs: object) -> object:
        raise KeyboardInterrupt

    monkeypatch.setattr(orchestrator, "_run_with_optional_subprocess_bridge", interrupted)

    with pytest.raises(KeyboardInterrupt):
        orchestrator.run_and_publish(confirm_repo="owner/dataset", tracker=tracker)

    logger.event.assert_called_once_with(
        "interrupted",
        level="WARNING",
        stage="run-and-publish",
    )
    logger.close.assert_not_called()
    tracker.finish.assert_called_once_with()


def test_run_and_publish_uses_the_stable_default_progress_interval(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    logger = Mock()
    captured: dict[str, object] = {}

    def resolved_clock() -> str:
        return "resolved"

    monkeypatch.setattr(orchestrator, "_resolve_clock", lambda _clock: resolved_clock)
    monkeypatch.setattr(
        orchestrator,
        "_ensure_logger",
        lambda _logger, **_kwargs: (logger, False),
    )

    def run_optional(_runner: object, **kwargs: object) -> object:
        captured.update(kwargs)
        return object()

    monkeypatch.setattr(orchestrator, "_run_with_optional_subprocess_bridge", run_optional)

    orchestrator.run_and_publish(confirm_repo="owner/dataset", logger=logger)

    assert captured["progress_interval"] == 100_000
