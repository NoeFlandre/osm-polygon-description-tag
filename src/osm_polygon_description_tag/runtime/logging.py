"""Typed operational event sink with bounded atomic rotation.

The :class:`RunLogger` is dependency-injected through the orchestrator,
build progress, publication retry, verification, and state-write
boundaries. It never relies on global logging state.

Two parallel streams are emitted for every event:

- a redacted, human-readable line on ``stderr`` (flushed immediately);
- a canonical JSON line appended to
  ``<data-root>/logs/run-and-publish.jsonl`` (flushed and fsynced).

Persistent events are buffered in memory until the orchestrator signals
that preflight succeeded. A denied preflight therefore preserves the
existing guarantee that no PBF or generated artifact is touched.

The JSONL log is rotated at a fixed size (10 MiB by default) with five
backups. Rotation is synchronous and atomic: the active file is fsynced,
a new owned empty active file is created, the previous file is
hard-linked into a same-directory staging slot, the backup chain is
shifted with :func:`os.replace`, the active path is swapped to the new
empty file, and the directory is fsynced. Interruption can leave a
staging file or a gap in backup numbering, but never a partial or
truncated archive.
"""

from __future__ import annotations

import contextlib
import json
import os
import re
import sys
import threading
import uuid
from collections.abc import Callable, Iterable, Mapping
from pathlib import Path
from typing import IO, Any

from osm_polygon_description_tag.runtime.time import utc_now_iso

_REDACTED = "[REDACTED]"

# Allowlisted field names safe to write through. Any key not in this
# set is dropped before the event is rendered (callers can opt in by
# passing the value through :meth:`RunLogger.event` as kwarg).
_ALLOWED_FIELDS: frozenset[str] = frozenset(
    {
        "ts",
        "event",
        "run_id",
        "source",
        "source_index",
        "source_total",
        "decision",
        "stage",
        "result",
        "reason",
        "level",
        "attempt",
        "delay_seconds",
        "kind",
        "exit_code",
        "rows",
        "bytes",
        "emitted",
        "included",
        "command_hash",
        "verified_revision",
        "identity_sha256",
        "size_bytes",
        "elapsed_seconds",
        "path",
        "operation",
        "safe_value",
        "total",
        "source_count",
        "per_pbf_uploads",
        "osmium_executable",
        "osmium_version",
        "hub_repo_sha",
    }
)

_CREDENTIAL_KEYS: frozenset[str] = frozenset(
    {"token", "bearer", "authorization", "auth", "password", "secret"}
)

_CREDENTIAL_RE = re.compile(
    r"(?i)(?:hf_[A-Za-z0-9]{8,}|Bearer\s+[A-Za-z0-9._\-]{4,}|Token\s+[A-Za-z0-9._\-]{4,})"
)


class _BufferedEvent:
    __slots__ = ("raw", "record")

    def __init__(self, record: dict[str, object], raw: str) -> None:
        self.record = record
        self.raw = raw


def _scrub_value(value: object) -> object:
    if isinstance(value, str) and _CREDENTIAL_RE.search(value):
        return _REDACTED
    return value


def _scrub(payload: dict[str, object]) -> dict[str, object]:
    cleaned: dict[str, object] = {}
    for key, value in payload.items():
        if not isinstance(key, str):
            continue
        if key in _CREDENTIAL_KEYS:
            cleaned[key] = _REDACTED
            continue
        if key in _ALLOWED_FIELDS:
            cleaned[key] = _scrub_value(value)
    return cleaned


def _ensure_log_directory(subdir: Path) -> None:
    if subdir.exists() or subdir.is_symlink():
        if subdir.is_symlink() or not subdir.is_dir():
            raise ValueError(f"logs path is not a regular directory: {subdir}")
        return
    subdir.mkdir(parents=True, exist_ok=False)


def _validate_active_log(active: Path) -> None:
    if active.is_symlink() or (active.exists() and not active.is_file()):
        raise ValueError(f"active log must be a regular file: {active}")


def _rotation_needed(path: Path, max_bytes: int) -> bool:
    return path.stat().st_size >= max_bytes


def _backup_chain(subdir: Path, backups: int) -> list[Path]:
    return [subdir / f"run-and-publish.{n}.jsonl" for n in range(1, backups + 1)]


def _validate_rotation_paths(active: Path, backup_chain: Iterable[Path]) -> None:
    for backup in backup_chain:
        if backup.is_symlink():
            raise ValueError(f"backup path is a symlink: {backup}")
    _validate_active_log(active)


def _shift_backups(backup_chain: list[Path]) -> None:
    for index in range(len(backup_chain) - 1, 0, -1):
        src = backup_chain[index - 1]
        dst = backup_chain[index]
        if src.exists() and src.is_file():
            os.replace(src, dst)


def _create_active_log(subdir: Path, active_name: str) -> Path:
    new_active = subdir / active_name
    fd = os.open(str(new_active), os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    os.close(fd)
    _fsync_directory(subdir)
    return new_active


def _fsync_directory(directory: Path) -> None:
    try:
        dir_fd = os.open(str(directory), os.O_RDONLY)
        try:
            os.fsync(dir_fd)
        finally:
            os.close(dir_fd)
    except OSError:
        pass


class RunLogger:
    """Typed event sink with bounded atomic rotation.

    The logger is single-run: a fresh run creates a new JSONL file (or
    rotates into a backup) at the canonical path. Backups are kept
    under a fixed chain and never reused.
    """

    ACTIVE_NAME = "run-and-publish.jsonl"
    SUBDIR_NAME = "logs"

    def __init__(
        self,
        *,
        data_root: Path,
        run_id: str,
        clock: Callable[[], str] | None = None,
        buffer_preflight: bool = False,
        stderr: Any | None = None,
        observer: Callable[[Mapping[str, object]], None] | None = None,
    ) -> None:
        self._data_root = data_root
        self._run_id = run_id
        self._clock: Callable[[], str] = clock or utc_now_iso
        self._lock = threading.Lock()
        self._buffered: list[_BufferedEvent] = []
        self._buffer_preflight = buffer_preflight
        self._stderr = stderr if stderr is not None else sys.stderr
        self._observer = observer
        self._max_bytes = 10 * 1024 * 1024
        self._backups = 5
        self._path: Path | None = None
        self._handle: IO[bytes] | None = None
        if not buffer_preflight:
            self._open_persistent()

    @property
    def run_id(self) -> str:
        return self._run_id

    def configure_rotation(self, *, max_bytes: int, backups: int) -> None:
        if max_bytes <= 0:
            raise ValueError("max_bytes must be positive")
        if backups < 0:
            raise ValueError("backups must be non-negative")
        with self._lock:
            self._max_bytes = max_bytes
            self._backups = backups

    def deny_preflight(self) -> None:
        """Drop any buffered events without opening the persistent file."""
        with self._lock:
            self._buffered.clear()
            self._buffer_preflight = True

    def approve_preflight(self) -> None:
        """Open the persistent file and flush buffered events."""
        with self._lock:
            self._buffer_preflight = False
            self._open_persistent()
            buffered = list(self._buffered)
            self._buffered.clear()
        for event in buffered:
            self._append_persistent(event.raw)

    def drain(self) -> Iterable[dict[str, object]]:
        with self._lock:
            buffered = list(self._buffered)
            self._buffered.clear()
        for event in buffered:
            yield event.record

    def event(self, name: str, *, level: str = "INFO", **fields: object) -> None:
        record: dict[str, object] = {
            "ts": self._clock(),
            "level": level,
            "event": name,
            "run_id": self._run_id,
        }
        for key, value in fields.items():
            record[key] = value
        scrubbed = _scrub(record)
        # pragma: no mutate start - ensure_ascii=None equals False; exact JSON bytes are tested
        raw = json.dumps(scrubbed, ensure_ascii=False, sort_keys=True, default=str)
        # pragma: no mutate end
        if self._observer is not None:
            with contextlib.suppress(Exception):
                self._observer(dict(scrubbed))
        self._emit(scrubbed, raw)

    def _emit(self, record: dict[str, object], raw: str) -> None:
        line = self._format_human(record)
        try:
            self._stderr.write(line + "\n")
            self._stderr.flush()
        except Exception:  # noqa: S110 - stderr is best-effort
            pass
        buffered = _BufferedEvent(record, raw)
        with self._lock:
            if self._buffer_preflight or self._handle is None:
                self._buffered.append(buffered)
                return
        self._append_persistent(raw)

    def _format_human(self, record: dict[str, object]) -> str:
        ts = record.get("ts", "")
        level = record.get("level", "INFO")
        name = record.get("event", "event")
        run_id = record.get("run_id", "")
        extras: list[str] = []
        for key, value in record.items():
            if key in {"ts", "level", "event", "run_id"}:
                continue
            extras.append(f"{key}={value}")
        suffix = " " + " ".join(extras) if extras else ""
        return f"{ts} {level} run={run_id} {name}{suffix}"

    def _open_persistent(self) -> None:
        if self._handle is not None:
            return
        subdir = self._data_root / self.SUBDIR_NAME
        _ensure_log_directory(subdir)
        active = subdir / self.ACTIVE_NAME
        _validate_active_log(active)
        self._path = active
        self._handle = open(active, "ab", buffering=0)  # noqa: SIM115

    def _append_persistent(self, raw: str) -> None:
        with self._lock:
            self._raw_write(raw)

    def append_raw(self, raw: str) -> None:
        """Append a pre-built raw line; reserved for tests and rotation."""
        with self._lock:
            if self._handle is None:
                raise ValueError("logger is not opened for persistent writes")
            self._raw_write(raw)

    def _raw_write(self, raw: str) -> None:
        assert self._handle is not None
        if not raw.endswith("\n"):
            raw = raw + "\n"
        # pragma: no mutate start - UTF-8 codec names are case-insensitive
        payload = raw.encode("utf-8")
        # pragma: no mutate end
        self._handle.write(payload)
        with contextlib.suppress(OSError):
            os.fsync(self._handle.fileno())
        assert self._path is not None
        if self._path.stat().st_size >= self._max_bytes:
            self._rotate_locked()

    def maybe_rotate(self) -> None:
        with self._lock:
            if self._handle is None or self._path is None:
                return
            _validate_active_log(self._path)
            if _rotation_needed(self._path, self._max_bytes):
                self._rotate_locked()

    def _close_handle(self) -> None:
        """Close the active handle while the caller holds the lock."""
        assert self._handle is not None
        self._handle.flush()
        with contextlib.suppress(OSError):
            os.fsync(self._handle.fileno())
        self._handle.close()
        self._handle = None

    def _rotate_locked(self) -> None:
        assert self._handle is not None
        assert self._path is not None
        self._close_handle()
        subdir = self._path.parent
        backup_chain = _backup_chain(subdir, self._backups)
        staging = subdir / f".run-and-publish.rotate.{uuid.uuid4().hex}.jsonl"
        _validate_rotation_paths(self._path, backup_chain)
        os.link(self._path, staging)
        self._path.unlink()
        _shift_backups(backup_chain)
        if backup_chain:
            os.replace(staging, backup_chain[0])
        else:
            staging.unlink()
        new_active = _create_active_log(subdir, self.ACTIVE_NAME)
        self._handle = open(new_active, "ab", buffering=0)  # noqa: SIM115
        self._path = new_active

    def flush(self) -> None:
        with self._lock:
            if self._handle is None:
                return
            self._handle.flush()
            with contextlib.suppress(OSError):
                os.fsync(self._handle.fileno())

    def close(self) -> None:
        with self._lock:
            if self._handle is not None:
                self._close_handle()


def configure_rotation(logger: RunLogger, *, max_bytes: int, backups: int) -> None:
    """Public rotation configuration helper used by tests and CLI wiring."""
    logger.configure_rotation(max_bytes=max_bytes, backups=backups)


__all__ = [
    "RunLogger",
    "configure_rotation",
]
