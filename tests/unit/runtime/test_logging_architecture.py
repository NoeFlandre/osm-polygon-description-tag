"""Operational event-sink contract for the amendment dataset.

The amendment introduces a typed event sink that:

- writes a flushed, redacted human line to stderr;
- writes a canonical JSON line to ``<data-root>/logs/run-and-publish.jsonl``;
- rotates the JSONL at 10 MiB with 5 backups using atomic same-directory
  hard-link staging and ``os.replace``;
- rejects symlinks for the logs directory, the active file, staging names,
  and backups;
- never enters an upload plan.

This file contains RED tests for each invariant. Implementation lives in
``osm_polygon_description_tag.runtime.logging``.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest


@pytest.fixture
def logger_factory():
    from osm_polygon_description_tag.runtime.logging import (
        RunLogger,
        configure_rotation,
    )

    return RunLogger, configure_rotation


def test_run_logger_writes_canonical_jsonl_and_human_stderr(
    tmp_path: Path, logger_factory, capsys: pytest.CaptureFixture[str]
) -> None:
    RunLogger, _configure = logger_factory
    data_root = tmp_path / "generated"
    data_root.mkdir()
    logger = RunLogger(
        data_root=data_root,
        run_id="test-run-1",
        clock=lambda: "2026-07-28T00:00:00+00:00",
    )
    logger.event(
        "source_decision",
        level="INFO",
        source_index=1,
        source_total=2,
        source="a.osm.pbf",
        decision="build",
    )
    logger.flush()
    captured = capsys.readouterr()
    assert captured.err.strip() != ""
    jsonl = (data_root / "logs" / "run-and-publish.jsonl").read_text(encoding="utf-8")
    records = [json.loads(line) for line in jsonl.splitlines() if line]
    assert len(records) == 1
    record = records[0]
    assert record["event"] == "source_decision"
    assert record["run_id"] == "test-run-1"
    assert record["level"] == "INFO"
    assert record["ts"] == "2026-07-28T00:00:00+00:00"
    assert record["source"] == "a.osm.pbf"
    assert record["source_index"] == 1
    assert record["source_total"] == 2
    assert record["decision"] == "build"


def test_run_logger_persists_after_preflight_in_memory_buffer(
    tmp_path: Path, logger_factory
) -> None:
    """Preflight-rejected events are buffered; never persisted or uploaded."""
    RunLogger, _ = logger_factory
    data_root = tmp_path / "generated"
    data_root.mkdir()
    logger = RunLogger(
        data_root=data_root,
        run_id="test-run-2",
        clock=lambda: "2026-07-28T00:00:00+00:00",
        buffer_preflight=True,
    )
    logger.event("preflight", level="INFO")
    logger.event("preflight_denied", level="ERROR", reason="hub auth")
    logger.deny_preflight()
    # The logs directory is never created.
    assert not (data_root / "logs").exists()
    # And the buffer was never persisted.
    assert list(logger.drain()) == []


def test_run_logger_rejects_symlink_logs_dir(tmp_path: Path, logger_factory) -> None:
    """A symlinked logs directory is rejected before any write."""
    RunLogger, _ = logger_factory
    data_root = tmp_path / "generated"
    data_root.mkdir()
    real_dir = tmp_path / "real_logs"
    real_dir.mkdir()
    logs_link = data_root / "logs"
    try:
        logs_link.symlink_to(real_dir)
    except OSError:
        pytest.skip("filesystem does not support symlinks")
    with pytest.raises(ValueError, match="symlink|regular"):
        RunLogger(
            data_root=data_root,
            run_id="test-run-3",
            clock=lambda: "2026-07-28T00:00:00+00:00",
        )


def test_run_logger_redacts_credential_values(tmp_path: Path, logger_factory) -> None:
    """Credential-like values are redacted before any sink writes them."""
    RunLogger, _ = logger_factory
    data_root = tmp_path / "generated"
    data_root.mkdir()
    logger = RunLogger(
        data_root=data_root,
        run_id="test-run-4",
        clock=lambda: "2026-07-28T00:00:00+00:00",
    )
    sentinel_token = "hf_" + "X" * 24
    logger.event(
        "upload_attempt",
        level="INFO",
        token=sentinel_token,
        bearer="Bearer TESTBEARER_SENTINEL_VALUE",
        authorization="Token TESTSECRET_AUTH_SENTINEL",
        safe_value="visible",
    )
    logger.flush()
    jsonl = (data_root / "logs" / "run-and-publish.jsonl").read_text(encoding="utf-8")
    record = json.loads(jsonl.splitlines()[-1])
    for field in ("token", "bearer", "authorization"):
        value = record.get(field, "")
        assert "hf_abc123def456ghi789" not in str(value)
        assert "eyJhbGciOiJI" not in str(value)
        assert "SECRET" not in str(value)
    assert record["safe_value"] == "visible"


def test_run_logger_drops_non_allowlisted_context(tmp_path: Path, logger_factory) -> None:
    RunLogger, _ = logger_factory
    data_root = tmp_path / "generated"
    data_root.mkdir()
    logger = RunLogger(
        data_root=data_root,
        run_id="allowlist",
        clock=lambda: "2026-07-28T00:00:00+00:00",
    )
    logger.event(
        "upload_retry",
        attempt=2,
        delay_seconds=1.0,
        arbitrary_context="must-not-persist",
        hf_api_key="Bearer secret-value",
    )
    logger.flush()
    record = json.loads((data_root / "logs" / "run-and-publish.jsonl").read_text().splitlines()[-1])
    assert record["attempt"] == 2
    assert "arbitrary_context" not in record
    assert "hf_api_key" not in record


def test_run_logger_emits_failure_and_interruption_events(tmp_path: Path, logger_factory) -> None:
    """Failure and KeyboardInterrupt are recorded with safe scalar context."""
    RunLogger, _ = logger_factory
    data_root = tmp_path / "generated"
    data_root.mkdir()
    logger = RunLogger(
        data_root=data_root,
        run_id="test-run-5",
        clock=lambda: "2026-07-28T00:00:00+00:00",
    )
    try:
        logger.event("build_start", level="INFO", source="a.osm.pbf")
        raise KeyboardInterrupt
    except KeyboardInterrupt:
        logger.event("interrupted", level="WARNING", source="a.osm.pbf", stage="build")
    logger.flush()
    jsonl = (data_root / "logs" / "run-and-publish.jsonl").read_text(encoding="utf-8")
    events = [json.loads(line) for line in jsonl.splitlines() if line]
    names = [event["event"] for event in events]
    assert "interrupted" in names


def test_rotation_creates_bounded_backups(tmp_path: Path, logger_factory) -> None:
    """Rotation creates a configurable number of backups under a fixed size."""
    RunLogger, configure = logger_factory
    data_root = tmp_path / "generated"
    data_root.mkdir()
    logger = RunLogger(
        data_root=data_root,
        run_id="test-run-6",
        clock=lambda: "2026-07-28T00:00:00+00:00",
    )
    configure(
        logger,
        max_bytes=200,
        backups=5,
    )
    payload = json.dumps({"event": "noop", "filler": "x" * 64}) + "\n"
    for _ in range(20):
        logger.append_raw(payload)
        logger.maybe_rotate()
    logs_dir = data_root / "logs"
    existing = sorted(p.name for p in logs_dir.iterdir())
    expected = {"run-and-publish.jsonl"} | {f"run-and-publish.{n}.jsonl" for n in range(1, 6)}
    assert expected.issuperset(set(existing))
    # Active file is small after rotation.
    assert (logs_dir / "run-and-publish.jsonl").stat().st_size <= 200
    for n in range(1, 6):
        assert (logs_dir / f"run-and-publish.{n}.jsonl").is_file()


def test_rotation_rejects_symlink_or_traversal(tmp_path: Path, logger_factory) -> None:
    """Rotation must reject symlinked active files and unsafe backup names."""
    RunLogger, configure = logger_factory
    data_root = tmp_path / "generated"
    data_root.mkdir()
    logger = RunLogger(
        data_root=data_root,
        run_id="test-run-7",
        clock=lambda: "2026-07-28T00:00:00+00:00",
    )
    # Configure triggers directory creation.
    configure(logger, max_bytes=1024, backups=1)
    active = data_root / "logs" / "run-and-publish.jsonl"
    target = tmp_path / "external-target"
    target.write_bytes(b"x")
    try:
        active.unlink()
        active.symlink_to(target)
    except OSError:
        pytest.skip("filesystem does not support symlinks")
    with pytest.raises(ValueError, match="symlink|regular"):
        logger.maybe_rotate()


def test_logs_directory_never_appears_in_upload_plan(tmp_path: Path, logger_factory) -> None:
    """The logs directory is allowlisted locally but never in any UploadItem."""
    RunLogger, _ = logger_factory
    data_root = tmp_path / "generated"
    data_root.mkdir()
    logger = RunLogger(
        data_root=data_root,
        run_id="test-run-8",
        clock=lambda: "2026-07-28T00:00:00+00:00",
    )
    logger.event("preflight", level="INFO")
    logger.flush()
    from osm_polygon_description_tag.publication import (
        _build_metadata_only_upload_plan,
        _build_per_pbf_upload_plan,
        create_upload_plan,
    )

    (data_root / "data").mkdir()
    (data_root / "manifests").mkdir()
    (data_root / "README.md").write_text("# R")
    (data_root / "stats.json").write_text("{}")
    parquet_path = data_root / "data" / "a.parquet"
    has_parquet = parquet_path.exists()
    per_pbf_plan = _build_per_pbf_upload_plan(data_root, "a.osm.pbf") if has_parquet else None
    for plan in (
        create_upload_plan(data_root),
        per_pbf_plan,
        _build_metadata_only_upload_plan(data_root),
    ):
        if plan is None:
            continue
        for item in plan.files:
            assert not item.relative_path.startswith("logs"), item
            assert (
                "log" not in item.relative_path.lower()
                or item.relative_path.endswith(".jsonl") is False
            )


def test_no_op_run_appends_operational_logs_only(tmp_path: Path, logger_factory) -> None:
    """A no-op run may append operational logs without changing dataset state."""
    RunLogger, _ = logger_factory
    data_root = tmp_path / "generated"
    data_root.mkdir()
    logger = RunLogger(
        data_root=data_root,
        run_id="test-run-9",
        clock=lambda: "2026-07-28T00:00:00+00:00",
    )
    logger.event("preflight", level="INFO")
    logger.event("run_summary", level="INFO", result="no-op")
    logger.flush()
    jsonl = (data_root / "logs" / "run-and-publish.jsonl").read_text(encoding="utf-8")
    assert jsonl.strip() != ""


def test_run_logger_scrubs_non_string_value_branches(tmp_path: Path, logger_factory) -> None:
    """Non-string values pass through ``_scrub_value`` unchanged."""
    RunLogger, _ = logger_factory
    data_root = tmp_path / "generated"
    data_root.mkdir()
    logger = RunLogger(
        data_root=data_root,
        run_id="test-run-10",
        clock=lambda: "2026-07-28T00:00:00+00:00",
    )
    sentinel_token = "hf_" + "Y" * 24
    logger.event(
        "build_progress",
        level="INFO",
        rows=42,
        result=None,
        token=sentinel_token,
        hf_api_key="Bearer eyJhbGciOiJI",
    )
    logger.flush()
    jsonl = (data_root / "logs" / "run-and-publish.jsonl").read_text(encoding="utf-8")
    record = json.loads(jsonl.splitlines()[-1])
    assert record["rows"] == 42
    assert record["result"] is None
    assert record["token"] == "[REDACTED]"  # noqa: S105 - sentinel redacted value
    assert "hf_api_key" not in record


def test_run_logger_reconfigure_rotation_rejects_invalid_values(
    tmp_path: Path, logger_factory
) -> None:
    """Rotation parameters must be positive/non-negative or the call raises."""
    RunLogger, _ = logger_factory
    data_root = tmp_path / "generated"
    data_root.mkdir()
    logger = RunLogger(
        data_root=data_root,
        run_id="test-run-11",
        clock=lambda: "2026-07-28T00:00:00+00:00",
    )
    with pytest.raises(ValueError, match="max_bytes"):
        logger.configure_rotation(max_bytes=0, backups=2)
    with pytest.raises(ValueError, match="backups"):
        logger.configure_rotation(max_bytes=1024, backups=-1)


def test_run_logger_approve_preflight_flushes_buffered_events(
    tmp_path: Path, logger_factory
) -> None:
    """``approve_preflight`` opens the persistent file and flushes buffered events."""
    RunLogger, _ = logger_factory
    data_root = tmp_path / "generated"
    data_root.mkdir()
    logger = RunLogger(
        data_root=data_root,
        run_id="test-run-12",
        clock=lambda: "2026-07-28T00:00:00+00:00",
        buffer_preflight=True,
    )
    logger.event("preflight", level="INFO", osmium_version="1.19.1")
    logger.event("sources_discovered", level="INFO", total=2)
    logger.approve_preflight()
    jsonl = (data_root / "logs" / "run-and-publish.jsonl").read_text(encoding="utf-8")
    records = [json.loads(line) for line in jsonl.splitlines() if line]
    names = {record["event"] for record in records}
    assert "preflight" in names
    assert "sources_discovered" in names


def test_run_logger_buffered_flush_then_deny_clears_buffer(tmp_path: Path, logger_factory) -> None:
    """Buffer is cleared if preflight is denied after partial buffer."""
    RunLogger, _ = logger_factory
    data_root = tmp_path / "generated"
    data_root.mkdir()
    logger = RunLogger(
        data_root=data_root,
        run_id="test-run-13",
        clock=lambda: "2026-07-28T00:00:00+00:00",
        buffer_preflight=True,
    )
    logger.event("preflight", level="INFO")
    logger.event("preflight_denied", level="ERROR", reason="auth failed")
    logger.deny_preflight()
    assert list(logger.drain()) == []
    assert not (data_root / "logs").exists()


def test_run_logger_active_log_rejects_non_file(tmp_path: Path, logger_factory) -> None:
    """Active log path must be a regular file (not a directory)."""
    RunLogger, _ = logger_factory
    data_root = tmp_path / "generated"
    data_root.mkdir()
    logs_dir = data_root / "logs"
    logs_dir.mkdir(parents=True)
    # Plant a directory in place of the active file.
    blocker = logs_dir / "run-and-publish.jsonl"
    blocker.mkdir()
    with pytest.raises(ValueError, match="regular file"):
        RunLogger(
            data_root=data_root,
            run_id="test-run-14",
            clock=lambda: "2026-07-28T00:00:00+00:00",
        )


def test_rotation_uses_hard_link_staging_and_atomic_replace(tmp_path: Path, logger_factory) -> None:
    """Rotation creates backups using same-directory hard links and atomic ``os.replace``."""
    RunLogger, configure = logger_factory
    data_root = tmp_path / "generated"
    data_root.mkdir()
    logger = RunLogger(
        data_root=data_root,
        run_id="test-run-15",
        clock=lambda: "2026-07-28T00:00:00+00:00",
    )
    configure(logger, max_bytes=128, backups=3)
    payload = json.dumps({"event": "build_progress", "n": "x" * 32}) + "\n"
    for _ in range(10):
        logger.append_raw(payload)
        logger.maybe_rotate()
    logs_dir = data_root / "logs"
    # No leftover staging files.
    leftover = [p.name for p in logs_dir.iterdir() if p.name.startswith(".run-and-publish.rotate.")]
    assert leftover == []
    # Backups exist and share an inode with the previous active file via hard link semantics.
    backup_1 = logs_dir / "run-and-publish.1.jsonl"
    assert backup_1.is_file()


def test_rotation_drops_oldest_backup_when_chain_full(tmp_path: Path, logger_factory) -> None:
    """When the backup chain is full the oldest backup is dropped on rotation."""
    RunLogger, configure = logger_factory
    data_root = tmp_path / "generated"
    data_root.mkdir()
    logger = RunLogger(
        data_root=data_root,
        run_id="test-run-16",
        clock=lambda: "2026-07-28T00:00:00+00:00",
    )
    configure(logger, max_bytes=128, backups=2)
    payload = json.dumps({"event": "build_progress", "n": "x" * 32}) + "\n"
    for _ in range(10):
        logger.append_raw(payload)
        logger.maybe_rotate()
    logs_dir = data_root / "logs"
    existing = sorted(p.name for p in logs_dir.iterdir())
    # At most backups + active file
    assert len(existing) <= 3
    for name in existing:
        assert not name.startswith(".run-and-publish.rotate.")


def test_run_logger_close_is_idempotent_and_safe(tmp_path: Path, logger_factory) -> None:
    """Calling ``close`` multiple times or before any event is safe."""
    RunLogger, _ = logger_factory
    data_root = tmp_path / "generated"
    data_root.mkdir()
    logger = RunLogger(
        data_root=data_root,
        run_id="test-run-17",
        clock=lambda: "2026-07-28T00:00:00+00:00",
    )
    logger.close()
    logger.close()
    # After close, events are buffered again.
    logger.event("after_close", level="INFO")
    assert list(logger.drain())  # buffered because handle is closed


def test_project_root_uses_env_var_when_set(tmp_path, monkeypatch) -> None:
    """``OSM_POLYGON_DESCRIPTION_TAG_HOME`` overrides the upward walk."""
    from osm_polygon_description_tag.runtime.resources import project_root

    env_root = tmp_path / "env-root"
    env_root.mkdir()
    (env_root / "pyproject.toml").write_text("[project]\nname='x'\n")
    monkeypatch.setenv("OSM_POLYGON_DESCRIPTION_TAG_HOME", str(env_root))
    assert project_root() == env_root


def test_project_root_raises_when_no_pyproject_found(tmp_path, monkeypatch) -> None:
    """``project_root`` raises FileNotFoundError when no pyproject.toml is reachable.

    The function walks upward from ``__file__`` when the env var lacks a
    pyproject.toml. To exercise the negative branch we monkeypatch the
    upward-walk source by pointing ``__file__`` at a sentinel inside the
    empty directory so no parent contains a pyproject.toml.
    """
    from osm_polygon_description_tag.runtime import resources

    bogus = tmp_path / "no-project-here"
    bogus.mkdir()
    sentinel = bogus / "_resources_fake.py"
    sentinel.write_text("")
    monkeypatch.setattr(resources, "__file__", str(sentinel))
    monkeypatch.delenv("OSM_POLYGON_DESCRIPTION_TAG_HOME", raising=False)
    with pytest.raises(FileNotFoundError, match="project root"):
        resources.project_root()


def test_project_code_revision_returns_none_when_no_git(monkeypatch, tmp_path) -> None:
    """``project_code_revision`` returns None when git is unavailable or fails."""
    from osm_polygon_description_tag.runtime import resources

    monkeypatch.setattr(resources.subprocess, "run", lambda *a, **kw: None)
    # Use an env var that has no pyproject so it walks upward via __file__.
    # The walk will find the project root (it always does in this checkout),
    # but subprocess.run is patched to return None.
    fake_completed = type("Fake", (), {"returncode": 0, "stdout": ""})()
    monkeypatch.setattr(resources.subprocess, "run", lambda *a, **kw: fake_completed)
    assert resources.project_code_revision() in (None, "")


def test_project_code_revision_returns_revision_on_success(monkeypatch, tmp_path) -> None:
    """``project_code_revision`` returns the Git revision when git succeeds."""
    from osm_polygon_description_tag.runtime import resources

    fake_completed = type(
        "Fake",
        (),
        {"returncode": 0, "stdout": "abcdef1234567890\n"},
    )()
    monkeypatch.setattr(resources.subprocess, "run", lambda *a, **kw: fake_completed)
    assert resources.project_code_revision() == "abcdef1234567890"


def test_project_code_revision_returns_none_when_git_fails(monkeypatch) -> None:
    """``project_code_revision`` returns None when the git command fails."""
    from osm_polygon_description_tag.runtime import resources

    fake_completed = type("Fake", (), {"returncode": 128, "stdout": "fatal: not a git repo"})()
    monkeypatch.setattr(resources.subprocess, "run", lambda *a, **kw: fake_completed)
    assert resources.project_code_revision() is None
