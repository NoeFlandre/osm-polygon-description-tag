"""Direct behavioral tests for the orchestration stage boundaries."""

from __future__ import annotations

from datetime import datetime
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from osm_polygon_description_tag.osm.discovery import Source
from osm_polygon_description_tag.runtime.config import Paths
from osm_polygon_description_tag.workflow import orchestrator, source_runner
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
    subprocess_runner = object()
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
    monkeypatch.setattr(orchestrator, "build_per_pbf_upload_plan", build_plan)
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
        subprocess_runner=subprocess_runner,
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
    assert execute_args[1]["subprocess_runner"] is subprocess_runner
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
        "build_per_pbf_upload_plan",
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


def test_run_and_publish_executes_stages_in_order_and_finishes_tracker(  # noqa: C901, PLR0915 - long test; TODO(#62) split it
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
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

    subprocess_runner = object()

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
        subprocess_runner=subprocess_runner,
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
            "subprocess_runner": subprocess_runner,
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
            "subprocess_runner": subprocess_runner,
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

    monkeypatch.setattr(source_runner, "_state_read_publication_state", read_state)
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

    monkeypatch.setattr(source_runner, "_state_read_publication_state", fail_read)
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
