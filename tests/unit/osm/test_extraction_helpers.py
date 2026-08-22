"""Focused behavioral coverage for the checked OSM extraction helpers."""

from __future__ import annotations

import subprocess
from queue import Queue
from threading import Thread
from types import SimpleNamespace
from typing import Any, ClassVar

import pytest

import osm_polygon_description_tag.osm.extraction as extraction
from osm_polygon_description_tag.osm.extraction import OsmiumExportError


def _copy_unescape_with_deadline(wire: bytes) -> bytes:
    results: Queue[bytes] = Queue()

    def worker() -> None:
        results.put(extraction._copy_unescape(wire))

    thread = Thread(target=worker, daemon=True)
    thread.start()
    thread.join(timeout=0.5)
    if thread.is_alive():
        pytest.fail("COPY unescape did not terminate")
    return results.get_nowait()


@pytest.mark.parametrize(
    ("wire", "decoded"),
    [
        (b"\\t", b"\t"),
        (b"\\n", b"\n"),
        (b"\\r", b"\r"),
        (b"\\b", b"\b"),
        (b"\\f", b"\f"),
        (b"\\v", b"\v"),
        (b"\\\\", b"\\"),
    ],
)
def test_copy_unescape_decodes_each_supported_postgres_escape(
    wire: bytes,
    decoded: bytes,
) -> None:
    assert _copy_unescape_with_deadline(wire) == decoded


def test_copy_unescape_preserves_unknown_and_trailing_backslashes() -> None:
    assert _copy_unescape_with_deadline(b"x\\ty") == b"x\ty"
    assert _copy_unescape_with_deadline(b"\\q") == b"\\q"
    assert _copy_unescape_with_deadline(b"trailing\\") == b"trailing\\"


def test_copy_parser_strips_only_trailing_line_endings() -> None:
    line = b"\n0103\tway\t42\t1\t1\t2026-01-01T00:00:00Z\t{}\r\n"

    record = extraction.parse_copy_record(line)

    assert record.geometry_ewkb_hex == "\n0103"
    assert record.osm_type == "way"
    assert record.osm_id == 42
    assert record.version == 1
    assert record.changeset == 1
    assert record.timestamp == "2026-01-01T00:00:00Z"
    assert record.tags == {}

    with pytest.raises(ValueError, match="tags"):
        extraction.parse_copy_record(line.rstrip(b"\r\n") + b"X")
    with pytest.raises(ValueError, match=r"expected 7 COPY fields, got 8"):
        extraction.parse_copy_record(line.rstrip(b"\r\n") + b"\t")


def test_copy_parser_reports_invalid_field_encoding_exactly() -> None:
    line = b"\xff\tway\t42\t1\t1\t2026-01-01T00:00:00Z\t{}\n"

    with pytest.raises(ValueError, match=r"^invalid COPY record field:"):
        extraction.parse_copy_record(line)


def test_nullable_helpers_decode_null_and_values() -> None:
    assert extraction._nullable_int(b"\\N") is None
    assert extraction._nullable_int(b"\\t42") == 42
    assert extraction._nullable_str(b"\\N") is None
    assert extraction._nullable_str(b"hello\\nworld") == "hello\nworld"


def test_parse_tags_requires_a_json_object() -> None:
    assert extraction._parse_tags(b"\\N") == {}

    with pytest.raises(ValueError, match=r"^tags JSON must be an object$"):
        extraction._parse_tags(b"[]")


def test_export_command_uses_osmium_by_default() -> None:
    assert extraction.export_command("source.osm.pbf", "config.json")[0] == "osmium"


def test_iter_records_continues_after_blank_lines() -> None:
    good = b"0103\tway\t42\t1\t1\t2026-01-01T00:00:00Z\t{}\n"

    records = list(extraction.iter_records([b"\n", b"  \n", good]))

    assert [record.osm_id for record in records] == [42]


def test_error_constructor_defaults_to_empty_stderr() -> None:
    assert OsmiumExportError("failed").stderr == b""


def test_decode_stderr_replaces_invalid_utf8_and_strips() -> None:
    assert extraction._decode_stderr(b"  bad-\xff\n") == "bad-�"


def test_drain_stderr_reads_fixed_chunks_caps_and_closes_stream() -> None:
    class Stream:
        def __init__(self) -> None:
            self.chunks = [b"ab", b"cdef", b""]
            self.read_sizes: list[int] = []
            self.closed = False

        def read(self, size: int) -> bytes:
            self.read_sizes.append(size)
            return self.chunks.pop(0)

        def close(self) -> None:
            self.closed = True

    stream = Stream()
    buffer = bytearray()

    extraction._drain_stderr(stream, buffer, 3)

    assert bytes(buffer) == b"abc"
    assert stream.read_sizes == [65536, 65536, 65536]
    assert stream.closed is True


class _RecordingThread:
    created: ClassVar[list[_RecordingThread]] = []

    def __init__(self, *, target: Any, args: tuple[Any, ...], daemon: bool) -> None:
        self.target = target
        self.args = args
        self.daemon = daemon
        self.started = False
        self.__class__.created.append(self)

    def start(self) -> None:
        self.started = True


def test_start_export_configures_popen_and_stderr_drain(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    stdout = object()
    stderr = object()
    seen: dict[str, Any] = {}

    class Popen:
        def __init__(self, command: tuple[str, ...], **kwargs: Any) -> None:
            seen["command"] = command
            seen["kwargs"] = kwargs
            self.stdout = stdout
            self.stderr = stderr

    _RecordingThread.created = []
    monkeypatch.setattr(extraction.subprocess, "Popen", Popen)
    monkeypatch.setattr(extraction, "Thread", _RecordingThread)

    proc, returned_stdout, drain, buffer = extraction._start_export(("osmium", "export"), 123)

    assert isinstance(proc, Popen)
    assert returned_stdout is stdout
    assert isinstance(drain, _RecordingThread)
    assert buffer == bytearray()
    assert seen == {
        "command": ("osmium", "export"),
        "kwargs": {
            "shell": False,
            "stdout": extraction.subprocess.PIPE,
            "stderr": extraction.subprocess.PIPE,
        },
    }
    assert drain.target is extraction._drain_stderr
    assert drain.args == (stderr, buffer, 123)
    assert drain.daemon is True
    assert drain.started is True


def test_start_export_reports_missing_binary_and_pipe_exactly(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def missing(*_args: Any, **_kwargs: Any) -> None:
        raise FileNotFoundError("missing")

    monkeypatch.setattr(extraction.subprocess, "Popen", missing)
    with pytest.raises(OsmiumExportError, match=r"^osmium executable not found: custom-osmium$"):
        extraction._start_export(("custom-osmium", "export"), 1)

    class PartialPopen:
        def __init__(self, stdout: Any, stderr: Any) -> None:
            self.stdout = stdout
            self.stderr = stderr

    monkeypatch.setattr(
        extraction.subprocess,
        "Popen",
        lambda *_args, **_kwargs: PartialPopen(None, object()),
    )
    with pytest.raises(OsmiumExportError, match=r"^could not attach osmium stdout/stderr pipes$"):
        extraction._start_export(("osmium", "export"), 1)

    monkeypatch.setattr(
        extraction.subprocess,
        "Popen",
        lambda *_args, **_kwargs: PartialPopen(object(), None),
    )
    with pytest.raises(OsmiumExportError, match=r"^could not attach osmium stdout/stderr pipes$"):
        extraction._start_export(("osmium", "export"), 1)


def test_stop_process_uses_kill_timeout_before_killing() -> None:
    calls: list[tuple[str, Any]] = []

    class Process:
        def terminate(self) -> None:
            calls.append(("terminate", None))

        def wait(self, **kwargs: Any) -> None:
            calls.append(("wait", kwargs.get("timeout")))
            if len(calls) == 2:
                raise subprocess.TimeoutExpired("osmium", 0.5)

        def kill(self) -> None:
            calls.append(("kill", None))

    extraction._stop_process(Process(), 0.5)

    assert calls == [("terminate", None), ("wait", 0.5), ("kill", None), ("wait", None)]


class _Stdout:
    def __init__(self) -> None:
        self.closed = False
        self.close_calls = 0

    def close(self) -> None:
        self.close_calls += 1
        self.closed = True


class _Drain:
    def __init__(self) -> None:
        self.join_timeouts: list[float | None] = []

    def join(self, *, timeout: float | None) -> None:
        self.join_timeouts.append(timeout)


class _Process:
    def __init__(self, return_code: int = 0) -> None:
        self.return_code = return_code
        self.wait_calls = 0

    def wait(self) -> int:
        self.wait_calls += 1
        return self.return_code


def test_stream_export_uses_default_command_and_bounded_cleanup(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    stdout = _Stdout()
    drain = _Drain()
    proc = _Process()
    calls: dict[str, Any] = {}

    def command(source: object, config: object, *, executable: str) -> tuple[str, ...]:
        calls["command"] = (source, config, executable)
        return ("osmium", "export")

    monkeypatch.setattr(extraction, "export_command", command)
    monkeypatch.setattr(
        extraction, "_start_export", lambda cmd, cap: (proc, stdout, drain, bytearray())
    )
    monkeypatch.setattr(extraction, "iter_records", lambda _stream: iter(()))

    assert list(extraction.stream_export("source", "config")) == []
    assert calls["command"] == ("source", "config", "osmium")
    assert stdout.close_calls == 1
    assert proc.wait_calls == 1
    assert drain.join_timeouts == [6.0]


def test_stream_export_stops_process_and_closes_stdout_on_downstream_failure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    stdout = _Stdout()
    drain = _Drain()
    proc = _Process()
    stop_calls: list[tuple[object, float]] = []
    monkeypatch.setattr(extraction, "export_command", lambda *_args, **_kwargs: ("osmium",))
    monkeypatch.setattr(
        extraction, "_start_export", lambda _cmd, _cap: (proc, stdout, drain, bytearray())
    )

    def fail(_stream: object):
        raise RuntimeError("consumer failed")

    monkeypatch.setattr(extraction, "iter_records", fail)
    monkeypatch.setattr(
        extraction, "_stop_process", lambda process, timeout: stop_calls.append((process, timeout))
    )

    with pytest.raises(RuntimeError, match="consumer failed"):
        list(extraction.stream_export("source", "config", kill_timeout=0.25))

    assert stop_calls == [(proc, 0.25)]
    assert stdout.close_calls == 1
    assert drain.join_timeouts == [1.25]


def test_osmium_version_uses_default_command_and_decodes_first_line(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    seen: dict[str, Any] = {}

    def run(command: list[str], **kwargs: Any) -> SimpleNamespace:
        seen["command"] = command
        seen["kwargs"] = kwargs
        return SimpleNamespace(stdout=b" osmium version 1.19 \nsecond\n")

    monkeypatch.setattr(extraction.subprocess, "run", run)

    assert extraction.osmium_version(timeout=2.5) == "osmium version 1.19"
    assert seen == {
        "command": ["osmium", "--version"],
        "kwargs": {"check": True, "capture_output": True, "timeout": 2.5},
    }


def test_osmium_version_default_timeout_is_ten_seconds(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    seen: dict[str, Any] = {}

    def run(_command: list[str], **kwargs: Any) -> SimpleNamespace:
        seen.update(kwargs)
        return SimpleNamespace(stdout=b"osmium version 1.19\n")

    monkeypatch.setattr(extraction.subprocess, "run", run)

    assert extraction.osmium_version() == "osmium version 1.19"
    assert seen["timeout"] == 10.0


def test_osmium_version_handles_empty_and_invalid_output() -> None:
    class Runner:
        def __init__(self) -> None:
            self.responses = [SimpleNamespace(stdout=b""), SimpleNamespace(stdout=b"\xff")]

        def __call__(self, *_args: Any, **_kwargs: Any) -> SimpleNamespace:
            return self.responses.pop(0)

    runner = Runner()
    original = extraction.subprocess.run
    try:
        extraction.subprocess.run = runner  # type: ignore[assignment]
        assert extraction.osmium_version() == ""
        assert extraction.osmium_version() == "�"
    finally:
        extraction.subprocess.run = original  # type: ignore[assignment]


def test_osmium_version_wraps_missing_binary_exactly(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def missing(*_args: Any, **_kwargs: Any) -> None:
        raise FileNotFoundError("missing")

    monkeypatch.setattr(extraction.subprocess, "run", missing)

    with pytest.raises(OsmiumExportError, match=r"^osmium executable not found: custom-osmium$"):
        extraction.osmium_version("custom-osmium")
