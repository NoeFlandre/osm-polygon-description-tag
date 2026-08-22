"""Checked streaming of versioned OSM area exports from ``osmium export``.

The ``osmium`` binary owns area assembly; this module owns the bounded stream,
the PostgreSQL COPY record parser, and typed failure handling. No command is
ever assembled through a shell.
"""

import json
import subprocess
from collections.abc import Iterator
from dataclasses import dataclass
from threading import Thread
from typing import IO

# ---------------------------------------------------------------------------
# Public types
# ---------------------------------------------------------------------------

#: Maximum bytes of child stderr retained for diagnostics.
STDERR_CAP_BYTES = 1 << 20


class OsmiumExportError(RuntimeError):
    """Raised when the ``osmium export`` subprocess fails or is interrupted."""

    def __init__(self, message: str, *, stderr: bytes = b"") -> None:
        super().__init__(message)
        self.stderr = stderr


@dataclass(frozen=True)
class ExportRecord:
    """One parsed PostgreSQL COPY row emitted by ``osmium export``."""

    geometry_ewkb_hex: str
    osm_type: str
    osm_id: int
    version: int | None
    changeset: int | None
    timestamp: str | None
    tags: dict[str, str]


# ---------------------------------------------------------------------------
# Command construction (no shell)
# ---------------------------------------------------------------------------


def export_command(
    source: object, config: object, *, executable: str = "osmium"
) -> tuple[str, ...]:
    """Return the argument array for ``osmium export`` in PostgreSQL COPY mode.

    The amendment relies on osmium's documented ``area_tags: true`` and
    ``linear_tags: true`` general handling. We additionally restrict
    output to polygon geometries so nodes, line outputs, and open
    ways never enter the COPY stream.
    """
    return (
        executable,
        "export",
        str(source),
        "--output-format",
        "pg",
        "--config",
        str(config),
        "--geometry-types",
        "polygon",
        "--output",
        "-",
    )


# ---------------------------------------------------------------------------
# PostgreSQL COPY text decoding
# ---------------------------------------------------------------------------


def _copy_unescape(data: bytes) -> bytes:
    """Decode the PostgreSQL COPY text backslash escapes for one field."""
    out = bytearray()
    i = 0
    n = len(data)
    while i < n:
        byte = data[i]
        if byte == 0x5C and i + 1 < n:  # backslash escape
            nxt = data[i + 1]
            mapped = {
                ord("t"): 0x09,
                ord("n"): 0x0A,
                ord("r"): 0x0D,
                ord("b"): 0x08,
                ord("f"): 0x0C,
                ord("v"): 0x0B,
                ord("\\"): 0x5C,
            }.get(nxt)
            if mapped is not None:
                out.append(mapped)
                i += 2
                continue
        out.append(byte)
        i += 1
    return bytes(out)


def _nullable_int(field: bytes) -> int | None:
    if field == b"\\N":
        return None
    # pragma: no mutate start - UTF-8 codec names are case-insensitive
    return int(_copy_unescape(field).decode("utf-8"))
    # pragma: no mutate end


def _nullable_str(field: bytes) -> str | None:
    if field == b"\\N":
        return None
    # pragma: no mutate start - UTF-8 codec names are case-insensitive
    return _copy_unescape(field).decode("utf-8")
    # pragma: no mutate end


def _parse_tags(field: bytes) -> dict[str, str]:
    if field == b"\\N":
        return {}
    try:
        # pragma: no mutate start - UTF-8 codec names are case-insensitive
        parsed = json.loads(_copy_unescape(field).decode("utf-8"))
        # pragma: no mutate end
    except json.JSONDecodeError as error:
        raise ValueError(f"invalid tags JSON: {error}") from error
    if not isinstance(parsed, dict):
        raise ValueError("tags JSON must be an object")
    return {str(key): str(value) for key, value in parsed.items()}


def parse_copy_record(line: bytes) -> ExportRecord:
    """Parse one PostgreSQL COPY data line into a typed :class:`ExportRecord`."""
    stripped = line.rstrip(b"\r\n")
    fields = stripped.split(b"\t")
    if len(fields) != 7:
        raise ValueError(f"expected 7 COPY fields, got {len(fields)}")
    geometry, osm_type, osm_id, version, changeset, timestamp, tags = fields
    try:
        # pragma: no mutate start - ASCII codec names are case-insensitive
        geometry_ewkb_hex = _copy_unescape(geometry).decode("ascii")
        # pragma: no mutate end
        # pragma: no mutate start - UTF-8 codec names are case-insensitive
        osm_type_text = _copy_unescape(osm_type).decode("utf-8")
        osm_id_value = int(_copy_unescape(osm_id).decode("utf-8"))
        # pragma: no mutate end
        return ExportRecord(
            geometry_ewkb_hex=geometry_ewkb_hex,
            osm_type=osm_type_text,
            osm_id=osm_id_value,
            version=_nullable_int(version),
            changeset=_nullable_int(changeset),
            timestamp=_nullable_str(timestamp),
            tags=_parse_tags(tags),
        )
    except UnicodeDecodeError as error:
        raise ValueError(f"invalid COPY record field: {error}") from error


def iter_records(stream: IO[bytes]) -> Iterator[ExportRecord]:
    """Yield parsed records from a bounded COPY byte stream."""
    for line_number, raw in enumerate(stream, start=1):
        if not raw.strip():
            continue
        try:
            yield parse_copy_record(raw)
        except ValueError as error:
            raise ValueError(f"invalid COPY record on line {line_number}: {error}") from error


# ---------------------------------------------------------------------------
# Checked subprocess streaming
# ---------------------------------------------------------------------------


def _drain_stderr(stream: IO[bytes], buffer: bytearray, cap: int) -> None:
    """Read child stderr to EOF, retaining at most ``cap`` bytes for diagnostics."""
    try:
        while True:
            chunk = stream.read(65536)
            if not chunk:
                # pragma: no mutate start - return and break both execute finally close
                break
                # pragma: no mutate end
            remaining = cap - len(buffer)
            # pragma: no mutate start - slicing at zero appends no bytes
            if remaining > 0:
                buffer.extend(chunk[:remaining])
            # pragma: no mutate end
    finally:
        stream.close()


def _decode_stderr(buffer: bytes) -> str:
    # pragma: no mutate start - codec names are case-insensitive
    return buffer.decode("utf-8", errors="replace").strip()
    # pragma: no mutate end


def stream_export(
    source: object,
    config: object,
    *,
    executable: str = "osmium",
    stderr_cap_bytes: int = STDERR_CAP_BYTES,
    kill_timeout: float = 5.0,
) -> Iterator[ExportRecord]:
    """Stream parsed records from ``osmium export`` with bounded stderr.

    Uses ``shell=False`` and an argument array, drains stderr on a dedicated
    thread capped at ``stderr_cap_bytes``, terminates the child on downstream
    failure or cancellation, and raises :class:`OsmiumExportError` on a missing
    binary or non-zero exit.
    """
    command = export_command(source, config, executable=executable)
    proc, stdout, drain, stderr_buffer = _start_export(command, stderr_cap_bytes)
    # pragma: no mutate start - wait always assigns the code before it is read
    return_code = -1
    # pragma: no mutate end
    try:
        yield from iter_records(stdout)
        stdout.close()
        return_code = proc.wait()
    except BaseException:
        # Downstream failure or generator close (cancellation): stop the child.
        _stop_process(proc, kill_timeout)
        raise
    finally:
        if not stdout.closed:
            stdout.close()
        drain.join(timeout=kill_timeout + 1.0)

    if return_code != 0:
        raise OsmiumExportError(
            f"osmium exited {return_code}: {_decode_stderr(bytes(stderr_buffer))}",
            stderr=bytes(stderr_buffer),
        )


def _start_export(
    command: tuple[str, ...],
    stderr_cap_bytes: int,
) -> tuple[subprocess.Popen[bytes], IO[bytes], Thread, bytearray]:
    try:
        proc = subprocess.Popen(  # noqa: S603 - controlled argument array, no shell
            command,
            shell=False,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
    except FileNotFoundError as error:
        raise OsmiumExportError(f"osmium executable not found: {command[0]}") from error
    stdout = proc.stdout
    stderr = proc.stderr
    if stdout is None or stderr is None:
        raise OsmiumExportError("could not attach osmium stdout/stderr pipes")
    stderr_buffer = bytearray()
    drain = Thread(
        target=_drain_stderr,
        args=(stderr, stderr_buffer, stderr_cap_bytes),
        daemon=True,
    )
    drain.start()
    return proc, stdout, drain, stderr_buffer


def _stop_process(proc: subprocess.Popen[bytes], kill_timeout: float) -> None:
    proc.terminate()
    try:
        proc.wait(timeout=kill_timeout)
    except subprocess.TimeoutExpired:
        proc.kill()
        proc.wait()


def osmium_version(executable: str = "osmium", *, timeout: float = 10.0) -> str:
    """Return the first line of ``<executable> --version`` output."""
    try:
        completed = subprocess.run(  # noqa: S603 - controlled argument array, no shell
            [executable, "--version"],
            check=True,
            capture_output=True,
            timeout=timeout,
        )
    except FileNotFoundError as error:
        raise OsmiumExportError(f"osmium executable not found: {executable}") from error
    lines = completed.stdout.splitlines()
    first = lines[0] if lines else b""
    # pragma: no mutate start - codec names are case-insensitive
    return first.decode("utf-8", errors="replace").strip()
    # pragma: no mutate end
