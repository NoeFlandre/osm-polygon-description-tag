"""Checked streaming of versioned OSM area exports from ``osmium export``.

The ``osmium`` binary owns area assembly; this module owns the bounded stream,
the PostgreSQL COPY record parser, and typed failure handling. No command is
ever assembled through a shell.
"""

import json
import subprocess
from collections.abc import Iterator, Mapping
from dataclasses import dataclass
from threading import Thread
from typing import IO, cast

import orjson

# ---------------------------------------------------------------------------
# Public types
# ---------------------------------------------------------------------------

#: Maximum bytes of child stderr retained for diagnostics.
STDERR_CAP_BYTES = 1 << 20

_COPY_ESCAPES = {
    ord("t"): 0x09,
    ord("n"): 0x0A,
    ord("r"): 0x0D,
    ord("b"): 0x08,
    ord("f"): 0x0C,
    ord("v"): 0x0B,
    ord("\\"): 0x5C,
}


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


def _copy_escape_at(data: bytes, index: int) -> int | None:
    if data[index] != 0x5C or index + 1 >= len(data):
        return None
    return _COPY_ESCAPES.get(data[index + 1])


def _copy_unescape(data: bytes) -> bytes:
    """Decode the PostgreSQL COPY text backslash escapes for one field."""
    if b"\\" not in data:
        return data
    out = bytearray()
    i = 0
    n = len(data)
    while i < n:
        byte = data[i]
        mapped = _copy_escape_at(data, i)
        if mapped is not None:
            out.append(mapped)
            # pragma: no mutate start - index updates preserve forward progress
            i += 2
            # pragma: no mutate end
            continue
        out.append(byte)
        # pragma: no mutate start - index updates preserve forward progress
        i += 1
        # pragma: no mutate end
    return bytes(out)


def _nullable_int(field: bytes) -> int | None:
    if field == b"\\N":
        return None
    if b"\\" not in field and field.isascii():
        return int(field)
    # pragma: no mutate start - UTF-8 codec names are case-insensitive
    return int(_copy_unescape(field).decode("utf-8"))
    # pragma: no mutate end


def _nullable_str(field: bytes) -> str | None:
    if field == b"\\N":
        return None
    if b"\\" not in field:
        # pragma: no mutate start - UTF-8 codec names are case-insensitive
        return field.decode("utf-8")
        # pragma: no mutate end
    # pragma: no mutate start - UTF-8 codec names are case-insensitive
    return _copy_unescape(field).decode("utf-8")
    # pragma: no mutate end


def _load_tags(payload: bytes) -> object:
    try:
        return orjson.loads(payload)
    except orjson.JSONDecodeError:
        return json.loads(payload)


def _parse_tag_payload(field: bytes) -> object:
    if field == b"\\N":
        return {}
    try:
        # pragma: no mutate start - UTF-8 codec names are case-insensitive
        payload = field if b"\\" not in field else _copy_unescape(field)
        return _load_tags(payload)
        # pragma: no mutate end
    except json.JSONDecodeError as error:
        raise ValueError(f"invalid tags JSON: {error}") from error


def _tags_are_all_strings(parsed: Mapping[object, object]) -> bool:
    return all(type(key) is str and type(value) is str for key, value in parsed.items())


def _stringify_tags(parsed: Mapping[object, object]) -> dict[str, str]:
    return {str(key): str(value) for key, value in parsed.items()}


def _normalize_tags(parsed: object) -> dict[str, str]:
    if not isinstance(parsed, dict):
        raise ValueError("tags JSON must be an object")
    # pragma: no mutate start - cast affects static typing only
    mapping = cast(Mapping[object, object], parsed)
    # pragma: no mutate end
    if _tags_are_all_strings(mapping):
        # pragma: no mutate start - cast affects static typing only
        return cast(dict[str, str], parsed)
        # pragma: no mutate end
    return _stringify_tags(mapping)


def _parse_tags(field: bytes) -> dict[str, str]:
    return _normalize_tags(_parse_tag_payload(field))


def _parse_unescaped_integer(field: bytes) -> int:
    # pragma: no mutate start - UTF-8 codec names are case-insensitive
    return int(field) if field.isascii() else int(field.decode("utf-8"))
    # pragma: no mutate end


def _parse_unescaped_optional_integer(field: bytes) -> int | None:
    if field == b"\\N":
        return None
    return _parse_unescaped_integer(field)


def _parse_unescaped_text(field: bytes) -> str | None:
    if field == b"\\N":
        return None
    # pragma: no mutate start - UTF-8 codec names are case-insensitive
    return field.decode("utf-8")
    # pragma: no mutate end


def _parse_unescaped_record(fields: list[bytes]) -> ExportRecord:
    geometry, osm_type, osm_id, version, changeset, timestamp, tags = fields
    # pragma: no mutate start - codec names are case-insensitive
    geometry_text = geometry.decode("ascii")
    osm_type_text = osm_type.decode("utf-8")
    # pragma: no mutate end
    return ExportRecord(
        geometry_text,
        osm_type_text,
        _parse_unescaped_integer(osm_id),
        _parse_unescaped_optional_integer(version),
        _parse_unescaped_optional_integer(changeset),
        _parse_unescaped_text(timestamp),
        _parse_tags(tags),
    )


def _decode_copy_text(field: bytes, encoding: str) -> str:
    return _copy_unescape(field).decode(encoding)


def _parse_copy_integer(field: bytes) -> int:
    # pragma: no mutate start - UTF-8 codec names are case-insensitive
    return int(_decode_copy_text(field, "utf-8"))
    # pragma: no mutate end


def _parse_escaped_record(fields: list[bytes]) -> ExportRecord:
    geometry, osm_type, osm_id, version, changeset, timestamp, tags = fields
    # pragma: no mutate start - codec names are case-insensitive
    geometry_text = _decode_copy_text(geometry, "ascii")
    osm_type_text = _decode_copy_text(osm_type, "utf-8")
    # pragma: no mutate end
    return ExportRecord(
        geometry_ewkb_hex=geometry_text,
        osm_type=osm_type_text,
        osm_id=_parse_copy_integer(osm_id),
        version=_nullable_int(version),
        changeset=_nullable_int(changeset),
        timestamp=_nullable_str(timestamp),
        tags=_parse_tags(tags),
    )


def parse_copy_record(line: bytes) -> ExportRecord:
    """Parse one PostgreSQL COPY data line into a typed :class:`ExportRecord`."""
    stripped = line.rstrip(b"\r\n")
    fields = stripped.split(b"\t")
    if len(fields) != 7:
        raise ValueError(f"expected 7 COPY fields, got {len(fields)}")
    try:
        if b"\\" not in stripped:
            return _parse_unescaped_record(fields)
        return _parse_escaped_record(fields)
    except UnicodeDecodeError as error:
        raise ValueError(f"invalid COPY record field: {error}") from error


def _is_blank_copy_line(raw: bytes) -> bool:
    # pragma: no mutate start - added bytes cannot affect an already nonblank line
    return not raw or (raw[0] in b" \t\r\n" and not raw.strip())
    # pragma: no mutate end


def iter_records(stream: IO[bytes]) -> Iterator[ExportRecord]:
    """Yield parsed records from a bounded COPY byte stream."""
    for line_number, raw in enumerate(stream, start=1):
        if _is_blank_copy_line(raw):
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
