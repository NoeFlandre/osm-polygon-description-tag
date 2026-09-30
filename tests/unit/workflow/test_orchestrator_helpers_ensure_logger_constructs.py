"""Direct behavioral tests for the orchestration stage boundaries."""

from __future__ import annotations

import inspect
import subprocess
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from osm_polygon_description_tag.runtime.config import Paths
from osm_polygon_description_tag.workflow import orchestrator
from osm_polygon_description_tag.workflow.source_runner import (
    OrchestratorError,
)
from tests.helpers.orchestration import RecordingLogger as _Logger
from tests.helpers.orchestration import source_runner_workspace as _workspace


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
    assert kwargs["runner"] is None
    retry_observer = kwargs["retry_observer"]
    retry_observer(attempt=2, reason="timeout")  # type: ignore[operator]
    assert logger.events == [("upload_retry", {"attempt": 2, "reason": "timeout"})]
    assert orchestrator._source_retry_observer(None) is None

    def subprocess_runner(_command: list[str]) -> None:
        return None

    orchestrator._run_default_source_upload(
        plan, timeout=None, logger=None, subprocess_runner=subprocess_runner
    )
    assert calls[1][1]["runner"] is subprocess_runner


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
        subprocess_runner=print,
    )
    default_upload.assert_called_once_with(
        plan, timeout=7.0, logger=logger, subprocess_runner=print
    )
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
    subprocess_runner = object()
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

    monkeypatch.setattr(orchestrator, "build_per_pbf_upload_plan", build_plan)
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
        subprocess_runner=subprocess_runner,
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
                    "subprocess_runner": subprocess_runner,
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
    subprocess_runner = object()

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
            subprocess_runner=subprocess_runner,
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
        subprocess_runner=subprocess_runner,
    )


def test_upload_final_metadata_uses_default_clock_and_canonical_validator(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    paths, _source = _workspace(tmp_path)
    verifier = object()
    subprocess_runner = object()

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
            subprocess_runner=subprocess_runner,
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
            "subprocess_runner": subprocess_runner,
        },
    )

    orchestrator._upload_final_metadata(
        paths,
        verifier=verifier,
        upload_runner=upload_runner,
        upload_timeout=None,
        clock=explicit_clock,
        logger=None,
        subprocess_runner=subprocess_runner,
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


def test_run_and_publish_forwards_all_options_and_closes_owned_resources(  # noqa: C901 - long test; TODO(#62) split it
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

    def run_and_publish(**kwargs: object) -> object:
        captured["run_and_publish"] = kwargs
        return report

    monkeypatch.setattr(orchestrator, "_resolve_clock", resolve_clock)
    monkeypatch.setattr(orchestrator, "_ensure_logger", ensure_logger)
    monkeypatch.setattr(orchestrator, "_run_and_publish", run_and_publish)

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
    assert captured["run_and_publish"] == {
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
        "subprocess_runner": subprocess_runner,
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

    monkeypatch.setattr(orchestrator, "_run_and_publish", interrupted)

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

    def run_and_publish(**kwargs: object) -> object:
        captured.update(kwargs)
        return object()

    monkeypatch.setattr(orchestrator, "_run_and_publish", run_and_publish)

    orchestrator.run_and_publish(confirm_repo="owner/dataset", logger=logger)

    assert captured["progress_interval"] == 100_000
