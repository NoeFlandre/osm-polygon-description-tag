"""Focused behavioral coverage for upload verification and retry helpers."""

from __future__ import annotations

import hashlib
import re
import subprocess
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

import osm_polygon_description_tag.publication.upload as upload
from osm_polygon_description_tag.publication.models import (
    PublicationError,
    UploadItem,
    UploadPlan,
)


def _plan(tmp_path: Path) -> UploadPlan:
    content = b"artifact"
    artifact = tmp_path / "artifact.txt"
    artifact.write_bytes(content)
    item = UploadItem(
        relative_path=artifact.name,
        size_bytes=len(content),
        sha256=hashlib.sha256(content).hexdigest(),
    )
    return UploadPlan(
        repo_id="example/repository",
        data_root=str(tmp_path),
        files=(item,),
        identity_sha256="plan-identity",
    )


def _exact(message: str) -> str:
    return rf"^{re.escape(message)}$"


def test_verify_item_identity_accepts_exact_file(tmp_path: Path) -> None:
    plan = _plan(tmp_path)
    item = plan.files[0]

    upload._verify_item_identity(plan, item.relative_path, item.size_bytes, item.sha256)


def test_verify_item_identity_reports_missing_file_exactly(tmp_path: Path) -> None:
    plan = _plan(tmp_path)

    with pytest.raises(
        PublicationError,
        match=_exact(f"artifact missing for upload: {tmp_path / 'missing.txt'}"),
    ):
        upload._verify_item_identity(plan, "missing.txt", 0, "0" * 64)


def test_verify_item_identity_reports_size_drift_exactly(tmp_path: Path) -> None:
    plan = _plan(tmp_path)
    item = plan.files[0]

    with pytest.raises(
        PublicationError,
        match=_exact(f"size drift for {tmp_path / item.relative_path}"),
    ):
        upload._verify_item_identity(plan, item.relative_path, item.size_bytes + 1, item.sha256)


def test_verify_item_identity_reports_checksum_drift_exactly(tmp_path: Path) -> None:
    plan = _plan(tmp_path)
    item = plan.files[0]

    with pytest.raises(
        PublicationError,
        match=_exact(f"checksum drift for {tmp_path / item.relative_path}"),
    ):
        upload._verify_item_identity(plan, item.relative_path, item.size_bytes, "0" * 64)


def test_classify_failure_reports_exception_without_completed_process() -> None:
    assert upload._classify_failure(RuntimeError("runner failed")) == (
        False,
        None,
        "exception",
    )


def test_classify_failure_preserves_missing_returncode_and_kind() -> None:
    completed = SimpleNamespace(stderr=b"")
    error = SimpleNamespace(completed=completed)

    assert upload._classify_failure(error) == (False, None, "exit_code")


def test_failure_details_reports_timeout_kind_exactly() -> None:
    assert upload._failure_details(subprocess.TimeoutExpired(["hf"], 1.0)) == (
        True,
        None,
        "timeout",
    )


def test_contains_timeout_handles_missing_and_invalid_stderr() -> None:
    assert upload._contains_timeout(SimpleNamespace()) is False
    assert upload._contains_timeout(SimpleNamespace(stderr=None)) is False
    assert upload._contains_timeout(SimpleNamespace(stderr=b"response \xff TIMEOUT")) is True
    assert upload._contains_timeout(SimpleNamespace(stderr=b"completed")) is False


def test_completed_process_handles_missing_attribute() -> None:
    assert upload._completed_process(object()) is None


def test_invoke_runner_forwards_injected_command_and_timeout() -> None:
    seen: list[tuple[list[str], float | None]] = []

    def runner(command: list[str], timeout: float | None) -> None:
        seen.append((command, timeout))

    upload._invoke_runner(["hf", "upload"], 12.5, runner)

    assert seen == [(["hf", "upload"], 12.5)]


def test_invoke_runner_uses_explicit_subprocess_options(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    seen: list[tuple[list[str], dict[str, Any]]] = []

    def run(command: list[str], **kwargs: Any) -> None:
        seen.append((command, kwargs))

    monkeypatch.setattr(upload.subprocess, "run", run)
    upload._invoke_runner(["hf", "upload"], 12.5, None)

    assert seen == [
        (
            ["hf", "upload"],
            {"check": True, "shell": False, "timeout": 12.5},
        )
    ]


def test_called_error_retry_requires_retryable_error_and_remaining_attempt() -> None:
    assert upload._called_error_retry(False, 0, 3, 1.0, 30.0) is None
    assert upload._called_error_retry(True, 3, 3, 1.0, 30.0) is None
    assert upload._called_error_retry(True, 0, 3, 1.0, 30.0) == (1.0, 1.0)


def test_sleep_before_retry_increments_attempt_and_multiplies_delay(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    sleeps: list[float] = []
    events: list[dict[str, object]] = []

    def observe(**event: object) -> None:
        events.append(event)

    monkeypatch.setattr(upload.time, "sleep", sleeps.append)

    result = upload._sleep_before_retry(
        4,
        2.0,
        (3.0, 4.0),
        "exit_code",
        429,
        observe,
    )

    assert result == (5, 8.0)
    assert sleeps == [3.0]
    assert events == [
        {
            "attempt": 5,
            "kind": "exit_code",
            "exit_code": 429,
            "delay_seconds": 3.0,
        }
    ]


def test_run_with_retry_retries_and_observes_bounded_delay(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    attempts = 0
    sleeps: list[float] = []
    events: list[dict[str, object]] = []

    def observe(**event: object) -> None:
        events.append(event)

    def runner(_command: list[str], _timeout: float | None) -> None:
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            completed = subprocess.CompletedProcess([], returncode=429)
            error = subprocess.CalledProcessError(429, [])
            error.completed = completed  # type: ignore[attr-defined]
            raise error

    monkeypatch.setattr(upload.time, "sleep", sleeps.append)
    upload._run_with_retry(
        ["hf", "upload"],
        max_retries=1,
        backoff_seconds=3.0,
        backoff_factor=2.0,
        backoff_cap_seconds=2.0,
        _runner=runner,
        retry_observer=observe,
    )

    assert attempts == 2
    assert sleeps == [2.0]
    assert events == [
        {
            "attempt": 1,
            "kind": "exit_code",
            "exit_code": 429,
            "delay_seconds": 2.0,
        }
    ]


def test_run_with_retry_forwards_command_timeout_and_injected_runner(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    sentinel = object()
    seen: list[tuple[list[str], float | None, object | None]] = []

    def invoke(command: list[str], timeout: float | None, runner: object | None) -> None:
        seen.append((command, timeout, runner))

    monkeypatch.setattr(upload, "_invoke_runner", invoke)
    upload._run_with_retry(["hf", "upload"], timeout=12.5, _runner=sentinel)  # type: ignore[arg-type]

    assert seen == [(["hf", "upload"], 12.5, sentinel)]


def test_require_confirmation_reports_exact_errors(tmp_path: Path) -> None:
    plan = _plan(tmp_path)

    with pytest.raises(
        PublicationError,
        match=_exact("confirmation required (must match freshly computed plan identity)"),
    ):
        upload._require_confirmation(plan, None)

    with pytest.raises(
        PublicationError,
        match=_exact("confirmation does not match plan identity (refusing to upload)"),
    ):
        upload._require_confirmation(plan, "wrong")


def test_run_default_upload_forwards_all_arguments(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    seen: list[tuple[list[str], dict[str, object]]] = []

    def observer(**_event: object) -> None:
        return None

    def runner(command: list[str], **kwargs: object) -> None:
        seen.append((command, kwargs))

    monkeypatch.setattr(upload, "_default_runner_with_retry", runner)
    upload._run_default_upload(["hf", "upload"], 12.5, observer)

    assert seen == [
        (
            ["hf", "upload"],
            {"timeout": 12.5, "retry_observer": observer},
        )
    ]


def test_run_default_upload_supports_legacy_runner_without_observer(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    seen: list[tuple[list[str], dict[str, object]]] = []

    def legacy_runner(command: list[str], **kwargs: object) -> None:
        seen.append((command, kwargs))
        if len(seen) == 1:
            raise TypeError("got an unexpected keyword argument 'retry_observer'")

    def observer(**_event: object) -> None:
        return None

    monkeypatch.setattr(upload, "_default_runner_with_retry", legacy_runner)
    upload._run_default_upload(["hf", "upload"], 12.5, observer)

    assert seen == [
        (
            ["hf", "upload"],
            {"timeout": 12.5, "retry_observer": seen[0][1]["retry_observer"]},
        ),
        (["hf", "upload"], {"timeout": 12.5}),
    ]


def test_run_default_upload_does_not_swallow_unrelated_type_errors(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls = 0

    def runner(_command: list[str], **_kwargs: object) -> None:
        nonlocal calls
        calls += 1
        raise TypeError("runner body failed")

    monkeypatch.setattr(upload, "_default_runner_with_retry", runner)

    with pytest.raises(TypeError, match="^runner body failed$"):
        upload._run_default_upload(["hf", "upload"], None, None)
    assert calls == 1


def test_execute_upload_forwards_default_runner_arguments(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    plan = _plan(tmp_path)
    seen: list[tuple[list[str], float | None, object | None]] = []

    def observer(**_event: object) -> None:
        return None

    def run_default(
        command: list[str], timeout: float | None, retry_observer: object | None
    ) -> None:
        seen.append((command, timeout, retry_observer))

    monkeypatch.setattr(upload, "_run_default_upload", run_default)
    upload.execute_upload(
        plan,
        confirmation=plan.identity_sha256,
        timeout=12.5,
        retry_observer=observer,
    )

    assert seen == [
        (
            [
                "hf",
                "upload-large-folder",
                plan.repo_id,
                str(tmp_path),
                "--repo-type",
                "dataset",
                "--include",
                "artifact.txt",
            ],
            12.5,
            observer,
        )
    ]
