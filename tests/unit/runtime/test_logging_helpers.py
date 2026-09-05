from __future__ import annotations

import json
from io import StringIO
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, call, patch

import pytest

import osm_polygon_description_tag.runtime.logging as logging_module


def _buffered_logger(tmp_path: Path) -> logging_module.RunLogger:
    return logging_module.RunLogger(
        data_root=tmp_path,
        run_id="helper-run",
        buffer_preflight=True,
        stderr=StringIO(),
        clock=lambda: "2026-08-22T00:00:00+00:00",
    )


def test_backup_chain_has_exact_one_based_bounded_names(tmp_path: Path) -> None:
    assert logging_module._backup_chain(tmp_path, 0) == []
    assert logging_module._backup_chain(tmp_path, 3) == [
        tmp_path / "run-and-publish.1.jsonl",
        tmp_path / "run-and-publish.2.jsonl",
        tmp_path / "run-and-publish.3.jsonl",
    ]


def test_ensure_log_directory_creates_missing_directory_with_safe_options() -> None:
    subdir = Mock()
    subdir.exists.return_value = False
    subdir.is_symlink.return_value = False

    logging_module._ensure_log_directory(subdir)

    subdir.mkdir.assert_called_once_with(parents=True, exist_ok=False)


def test_ensure_log_directory_accepts_existing_directory_and_rejects_files(
    tmp_path: Path,
) -> None:
    existing = tmp_path / "logs"
    existing.mkdir()
    logging_module._ensure_log_directory(existing)

    blocker = tmp_path / "blocker"
    blocker.write_text("not a directory", encoding="utf-8")
    with pytest.raises(
        ValueError,
        match=rf"logs path is not a regular directory: {blocker}",
    ):
        logging_module._ensure_log_directory(blocker)


def test_ensure_log_directory_rejects_a_symlink_before_directory_checks(
    tmp_path: Path,
) -> None:
    real_dir = tmp_path / "real"
    real_dir.mkdir()
    link = tmp_path / "logs"
    try:
        link.symlink_to(real_dir)
    except OSError:
        pytest.skip("filesystem does not support symlinks")

    with pytest.raises(ValueError, match=rf"logs path is not a regular directory: {link}"):
        logging_module._ensure_log_directory(link)


def test_fsync_directory_opens_fsyncs_and_closes_the_directory() -> None:
    directory = Path("logs")
    with (
        patch.object(logging_module.os, "open", return_value=17) as open_directory,
        patch.object(logging_module.os, "fsync") as fsync,
        patch.object(logging_module.os, "close") as close,
    ):
        logging_module._fsync_directory(directory)

    open_directory.assert_called_once_with(str(directory), logging_module.os.O_RDONLY)
    fsync.assert_called_once_with(17)
    close.assert_called_once_with(17)


def test_fsync_directory_swallows_os_errors_from_open() -> None:
    with patch.object(logging_module.os, "open", side_effect=OSError("no fsync")):
        logging_module._fsync_directory(Path("logs"))


def test_rotation_needed_includes_the_exact_size_boundary() -> None:
    path = Mock()
    path.stat.return_value.st_size = 100

    assert logging_module._rotation_needed(path, 100) is True
    assert logging_module._rotation_needed(path, 101) is False


def test_scrub_skips_non_string_keys_without_stopping_later_fields() -> None:
    assert logging_module._scrub({1: "drop", "safe_value": "keep"}) == {"safe_value": "keep"}


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        (42, 42),
        ("ordinary text", "ordinary text"),
        ("Bearer abcdefgh", "[REDACTED]"),
    ],
)
def test_scrub_value_handles_non_credentials_and_credential_like_strings(
    value: object,
    expected: object,
) -> None:
    assert logging_module._scrub_value(value) == expected


def test_shift_backups_does_not_replace_a_directory(tmp_path: Path) -> None:
    source = tmp_path / "run-and-publish.1.jsonl"
    destination = tmp_path / "run-and-publish.2.jsonl"
    source.mkdir()
    destination.write_text("old", encoding="utf-8")

    logging_module._shift_backups([source, destination])

    assert source.is_dir()
    assert destination.read_text(encoding="utf-8") == "old"


def test_validate_rotation_paths_checks_backup_symlinks_and_active_path() -> None:
    backup = Mock()
    backup.is_symlink.return_value = False
    active = Path("active.jsonl")

    with patch.object(logging_module, "_validate_active_log") as validate_active:
        logging_module._validate_rotation_paths(active, [backup])

    validate_active.assert_called_once_with(active)


def test_validate_rotation_paths_rejects_a_backup_symlink() -> None:
    backup = Mock()
    backup.is_symlink.return_value = True
    backup.__str__ = lambda _self: "backup.jsonl"

    with pytest.raises(ValueError, match=r"backup path is a symlink: backup\.jsonl"):
        logging_module._validate_rotation_paths(Path("active.jsonl"), [backup])


def test_buffered_event_preserves_record_and_raw_payload() -> None:
    record = {"event": "x"}
    event = logging_module._BufferedEvent(record, "raw")

    assert event.record is record
    assert event.raw == "raw"


def test_logger_initial_defaults_are_stable(tmp_path: Path) -> None:
    logger = _buffered_logger(tmp_path)

    assert logger._buffer_preflight is True
    assert logger._max_bytes == 10 * 1024 * 1024
    assert logger._backups == 5
    assert logger._path is None
    assert logger._handle is None


def test_close_flushes_syncs_closes_and_clears_handle(tmp_path: Path) -> None:
    logger = _buffered_logger(tmp_path)
    handle = Mock()
    handle.fileno.return_value = 23
    logger._handle = handle

    def assert_active_handle_locked() -> None:
        assert logger._lock.locked()
        assert logger._handle is handle

    handle.flush.side_effect = assert_active_handle_locked
    handle.close.side_effect = assert_active_handle_locked
    with patch.object(logging_module.os, "fsync") as fsync:
        handle.attach_mock(fsync, "fsync")
        logger.close()

    assert handle.mock_calls == [call.flush(), call.fileno(), call.fsync(23), call.close()]
    assert logger._handle is None
    assert not logger._lock.locked()


@pytest.mark.parametrize("operation", ["close", "maybe_rotate"])
@pytest.mark.parametrize(
    ("stage", "error_type"),
    [("flush", OSError), ("fsync", RuntimeError), ("close", OSError)],
)
def test_close_and_rotation_preserve_active_state_on_failure(
    tmp_path: Path,
    operation: str,
    stage: str,
    error_type: type[Exception],
) -> None:
    logger = _buffered_logger(tmp_path)
    active = tmp_path / "active.jsonl"
    active.write_bytes(b"existing log\n")
    handle = Mock()
    handle.fileno.return_value = 23
    logger._handle = handle
    logger._path = active
    logger.configure_rotation(max_bytes=1, backups=1)
    error = error_type(f"{stage} failed")
    if stage != "fsync":
        getattr(handle, stage).side_effect = error

    with (
        patch.object(logging_module.os, "fsync", side_effect=error if stage == "fsync" else None),
        pytest.raises(error_type) as raised,
    ):
        getattr(logger, operation)()

    assert raised.value is error
    assert logger._handle is handle
    assert logger._path == active
    assert active.read_bytes() == b"existing log\n"
    assert not logger._lock.locked()
    if stage != "close":
        handle.close.assert_not_called()


def test_emit_writes_one_human_line_and_buffers_the_original_record(tmp_path: Path) -> None:
    stderr = StringIO()
    logger = logging_module.RunLogger(
        data_root=tmp_path,
        run_id="emit-run",
        buffer_preflight=True,
        stderr=stderr,
    )
    record = {
        "ts": "t",
        "level": "INFO",
        "event": "source_decision",
        "run_id": "emit-run",
    }

    logger._emit(record, '{"event":"source_decision"}')

    assert stderr.getvalue() == "t INFO run=emit-run source_decision\n"
    assert list(logger.drain()) == [record]


def test_raw_write_normalizes_only_missing_newlines_and_uses_utf8(tmp_path: Path) -> None:
    logger = logging_module.RunLogger(
        data_root=tmp_path,
        run_id="raw-run",
        stderr=StringIO(),
    )
    logger.append_raw("é")
    logger.append_raw("done\n")
    logger.close()

    assert (tmp_path / "logs" / "run-and-publish.jsonl").read_bytes() == ("é\ndone\n".encode())


def test_raw_write_swallows_fsync_os_errors(tmp_path: Path) -> None:
    logger = logging_module.RunLogger(
        data_root=tmp_path,
        run_id="raw-sync-run",
        stderr=StringIO(),
    )
    with patch.object(logging_module.os, "fsync", side_effect=OSError("sync failed")):
        logger.append_raw("line")
    logger.close()

    assert (tmp_path / "logs" / "run-and-publish.jsonl").read_bytes() == b"line\n"


def test_raw_write_rotates_at_exact_maximum_size(tmp_path: Path) -> None:
    logger = logging_module.RunLogger(
        data_root=tmp_path,
        run_id="raw-rotate-run",
        stderr=StringIO(),
    )
    logger.configure_rotation(max_bytes=5, backups=1)

    logger.append_raw("1234")
    logger.close()

    logs = tmp_path / "logs"
    assert (logs / "run-and-publish.1.jsonl").read_bytes() == b"1234\n"
    assert (logs / "run-and-publish.jsonl").read_bytes() == b""


def test_append_raw_reports_exact_error_when_not_opened(tmp_path: Path) -> None:
    logger = _buffered_logger(tmp_path)

    with pytest.raises(ValueError, match=r"^logger is not opened for persistent writes$"):
        logger.append_raw("raw")


def test_maybe_rotate_forwards_all_paths_and_reopens_active_file(tmp_path: Path) -> None:
    logger = _buffered_logger(tmp_path)
    logs_dir = tmp_path / "logs"
    logs_dir.mkdir()
    active = logs_dir / logger.ACTIVE_NAME
    active.write_bytes(b"old")
    old_handle = Mock()
    old_handle.fileno.return_value = 31
    new_handle = Mock()
    new_active = logs_dir / logger.ACTIVE_NAME
    backup_chain = [
        logs_dir / "run-and-publish.1.jsonl",
        logs_dir / "run-and-publish.2.jsonl",
    ]
    logger._path = active
    logger._handle = old_handle
    logger.configure_rotation(max_bytes=1, backups=2)

    def assert_active_handle_locked() -> None:
        assert logger._lock.locked()
        assert logger._handle is old_handle

    old_handle.flush.side_effect = assert_active_handle_locked
    old_handle.close.side_effect = assert_active_handle_locked

    with (
        patch.object(logging_module.uuid, "uuid4", return_value=SimpleNamespace(hex="abc")),
        patch.object(logging_module, "_backup_chain", return_value=backup_chain) as backup,
        patch.object(logging_module, "_validate_rotation_paths") as validate_paths,
        patch.object(logging_module.os, "link") as link,
        patch.object(logging_module, "_shift_backups") as shift,
        patch.object(logging_module.os, "replace") as replace,
        patch.object(logging_module, "_create_active_log", return_value=new_active) as create,
        patch("builtins.open", return_value=new_handle) as open_active,
        patch.object(logging_module.os, "fsync") as fsync,
    ):
        logger.maybe_rotate()

    staging = logs_dir / ".run-and-publish.rotate.abc.jsonl"
    backup.assert_called_once_with(logs_dir, 2)
    validate_paths.assert_called_once_with(active, backup_chain)
    link.assert_called_once_with(active, staging)
    shift.assert_called_once_with(backup_chain)
    replace.assert_called_once_with(staging, backup_chain[0])
    create.assert_called_once_with(logs_dir, logger.ACTIVE_NAME)
    open_active.assert_called_once_with(new_active, "ab", buffering=0)
    old_handle.flush.assert_called_once_with()
    fsync.assert_called_once_with(31)
    old_handle.close.assert_called_once_with()
    assert logger._handle is new_handle
    assert logger._path == new_active


def test_approve_preflight_switches_to_persistent_mode_and_flushes_buffer(
    tmp_path: Path,
) -> None:
    logger = _buffered_logger(tmp_path)
    logger.event("before_approval")

    logger.approve_preflight()
    assert logger._buffer_preflight is False
    assert logger._handle is not None
    logger.event("after_approval")
    logger.close()

    events = [
        json.loads(line)
        for line in (tmp_path / "logs" / logger.ACTIVE_NAME).read_text().splitlines()
    ]
    assert [event["event"] for event in events] == ["before_approval", "after_approval"]


def test_deny_preflight_keeps_buffering_enabled_after_clearing(tmp_path: Path) -> None:
    logger = _buffered_logger(tmp_path)
    logger.event("discarded")

    logger.deny_preflight()
    assert logger._buffer_preflight is True
    logger.event("retained_in_memory")

    assert [event["event"] for event in logger.drain()] == ["retained_in_memory"]
    assert not (tmp_path / "logs").exists()


def test_configure_rotation_error_messages_are_exact(tmp_path: Path) -> None:
    logger = _buffered_logger(tmp_path)

    with pytest.raises(ValueError, match=r"^max_bytes must be positive$"):
        logger.configure_rotation(max_bytes=0, backups=1)
    with pytest.raises(ValueError, match=r"^backups must be non-negative$"):
        logger.configure_rotation(max_bytes=1, backups=-1)


def test_close_swallows_fsync_os_errors(tmp_path: Path) -> None:
    logger = logging_module.RunLogger(
        data_root=tmp_path,
        run_id="close-sync-run",
        stderr=StringIO(),
    )
    handle = logger._handle
    assert handle is not None
    with patch.object(logging_module.os, "fsync", side_effect=OSError("sync failed")):
        logger.close()

    assert handle.closed
    assert logger._handle is None


def test_approve_and_deny_locking_state_is_safe_for_follow_up_events(tmp_path: Path) -> None:
    logger = _buffered_logger(tmp_path)
    logger.deny_preflight()
    logger.event("still_buffered")
    assert next(iter(logger.drain()))["event"] == "still_buffered"

    logger.approve_preflight()
    logger.close()


def test_event_serialization_is_unicode_safe_sorted_and_stringifies_unknown_objects(
    tmp_path: Path,
) -> None:
    class Marker:
        def __str__(self) -> str:
            return "marker"

    logger = logging_module.RunLogger(
        data_root=tmp_path,
        run_id="json-run",
        clock=lambda: "t",
        stderr=StringIO(),
    )
    logger.event("unicode", safe_value="é", result={"z": 1, "a": Marker()})
    logger.close()

    raw = (tmp_path / "logs" / logger.ACTIVE_NAME).read_text(encoding="utf-8").strip()
    assert raw == (
        '{"event": "unicode", "level": "INFO", "result": {"a": "marker", "z": 1}, '
        '"run_id": "json-run", "safe_value": "é", "ts": "t"}'
    )


def test_flush_is_a_no_op_without_handle_and_swallows_sync_errors(tmp_path: Path) -> None:
    buffered = _buffered_logger(tmp_path)
    buffered.flush()

    logger = logging_module.RunLogger(
        data_root=tmp_path / "persistent",
        run_id="flush-run",
        stderr=StringIO(),
    )
    with patch.object(logging_module.os, "fsync", side_effect=OSError("sync failed")):
        logger.flush()
    logger.close()


def test_maybe_rotate_returns_when_handle_is_closed_even_if_path_remains(
    tmp_path: Path,
) -> None:
    logger = logging_module.RunLogger(
        data_root=tmp_path,
        run_id="maybe-run",
        stderr=StringIO(),
    )
    logger.event("needs-no-rotation")
    logger.configure_rotation(max_bytes=1, backups=0)
    logger.close()

    logger.maybe_rotate()
