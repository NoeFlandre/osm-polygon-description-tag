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
from collections.abc import Callable, Iterable
from datetime import UTC, datetime
from pathlib import Path
from typing import IO, Any

_REDACTED = "[REDACTED]"

# Allowlisted field names safe to write through. Any key not in this
# set is dropped before the event is rendered (callers can opt in by
# passing the value through :meth:`RunLogger.event` as kwarg).
_ALLOWED_FIELDS: frozenset[str] = frozenset(
    {
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
    }
)

_CREDENTIAL_KEYS: frozenset[str] = frozenset(
    {"token", "bearer", "authorization", "auth", "password", "secret"}
)

_CREDENTIAL_RE = re.compile(
    r"(?i)(?:hf_[A-Za-z0-9]{8,}|Bearer\s+[A-Za-z0-9._\-]{4,}|Token\s+[A-Za-z0-9._\-]{4,})"
)


class _SafeJsonFormatter(json.JSONEncoder):
    def default(self, obj: object) -> object:
        return str(obj)


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
        else:
            cleaned[key] = _scrub_value(value)
    return cleaned


def _utcnow_iso() -> str:
    return datetime.now(UTC).isoformat()


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
    ) -> None:
        self._data_root = data_root
        self._run_id = run_id
        self._clock: Callable[[], str] = clock or _utcnow_iso
        self._lock = threading.Lock()
        self._buffered: list[_BufferedEvent] = []
        self._buffer_preflight = buffer_preflight
        self._stderr = stderr if stderr is not None else sys.stderr
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
            if not isinstance(key, str):
                continue
            record[key] = value
        scrubbed = _scrub(record)
        raw = json.dumps(scrubbed, ensure_ascii=False, sort_keys=True, cls=_SafeJsonFormatter)
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
        if subdir.exists() or subdir.is_symlink():
            if subdir.is_symlink() or not subdir.is_dir():
                raise ValueError(f"logs path is not a regular directory: {subdir}")
        else:
            subdir.mkdir(parents=True, exist_ok=False)
        active = subdir / self.ACTIVE_NAME
        if active.is_symlink() or (active.exists() and not active.is_file()):
            raise ValueError(f"active log must be a regular file: {active}")
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
        payload = raw.encode("utf-8")
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
            if self._path.is_symlink() or not self._path.is_file():
                raise ValueError(f"active log must be a regular file: {self._path}")
            if self._path.stat().st_size >= self._max_bytes:
                self._rotate_locked()

    def _rotate_locked(self) -> None:
        assert self._handle is not None
        assert self._path is not None
        self._handle.flush()
        with contextlib.suppress(OSError):
            os.fsync(self._handle.fileno())
        self._handle.close()
        self._handle = None
        subdir = self._path.parent
        backup_chain = [subdir / f"run-and-publish.{n}.jsonl" for n in range(1, self._backups + 1)]
        staging = subdir / f".run-and-publish.rotate.{uuid.uuid4().hex}.jsonl"
        # Reject any unsafe state up front.
        for backup in backup_chain:
            if backup.is_symlink():
                raise ValueError(f"backup path is a symlink: {backup}")
        if self._path.is_symlink() or not self._path.is_file():
            raise ValueError(f"active log must be a regular file: {self._path}")
        # Stage a hard link to the current active file, never copy contents.
        os.link(self._path, staging)
        # Remove the active file so the chain replaces can swap in.
        self._path.unlink()
        # Shift backwards: replace (n) -> (n+1) freeing slot 1.
        for index in range(self._backups - 1, 0, -1):
            src = backup_chain[index - 1]
            dst = backup_chain[index]
            if not src.exists() or not src.is_file():
                continue
            os.replace(src, dst)
        os.replace(staging, backup_chain[0])
        # Recreate a fresh empty active file owned by this run.
        new_active = subdir / self.ACTIVE_NAME
        fd = os.open(str(new_active), os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        os.close(fd)
        try:
            dir_fd = os.open(str(subdir), os.O_RDONLY)
            try:
                os.fsync(dir_fd)
            finally:
                os.close(dir_fd)
        except OSError:
            pass
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
                self._handle.flush()
                with contextlib.suppress(OSError):
                    os.fsync(self._handle.fileno())
                self._handle.close()
                self._handle = None


def configure_rotation(logger: RunLogger, *, max_bytes: int, backups: int) -> None:
    """Public rotation configuration helper used by tests and CLI wiring."""
    logger.configure_rotation(max_bytes=max_bytes, backups=backups)


__all__ = [
    "RunLogger",
    "configure_rotation",
]
