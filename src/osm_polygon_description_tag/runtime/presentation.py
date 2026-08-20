"""Interactive stderr presentation for redacted operational events."""

from __future__ import annotations

import sys
from collections.abc import Mapping
from typing import TextIO

from rich.console import Console
from tqdm import tqdm


class TerminalPresenter:
    """Render progress and errors without writing stdout or durable state."""

    def __init__(self, *, stderr: TextIO | None = None) -> None:
        self._stderr = stderr if stderr is not None else sys.stderr
        self._interactive = bool(getattr(self._stderr, "isatty", lambda: False)())
        self._console = Console(
            file=self._stderr,
            force_terminal=self._interactive,
            color_system="auto",
        )
        self._progress: tqdm | None = None

    @property
    def progress_active(self) -> bool:
        return self._progress is not None

    def observe(self, event: Mapping[str, object]) -> None:
        """Update interactive progress from one redacted logger event."""
        if not self._interactive:
            return
        name = event.get("event")
        if name == "build_start":
            self._start_progress(event)
        elif name == "build_progress":
            self._update_progress(event)
        elif name in {"build_complete", "interrupted"}:
            self.close()

    def _start_progress(self, event: Mapping[str, object]) -> None:
        self.close()
        self._progress = tqdm(
            total=None,
            desc=str(event.get("source", "")),
            unit="features",
            file=self._stderr,
            disable=False,
        )

    def _update_progress(self, event: Mapping[str, object]) -> None:
        if self._progress is None:
            return
        value = event.get("emitted", 0)
        emitted = value if isinstance(value, int) else 0
        self._progress.update(max(0, emitted - self._progress.n))

    def error(self, message: str) -> None:
        """Render one domain error to stderr."""
        if self._interactive:
            self._console.print(f"[bold red]error:[/bold red] {message}")
        else:
            self._stderr.write(f"error: {message}\n")
            self._stderr.flush()

    def close(self) -> None:
        if self._progress is not None:
            self._progress.close()
            self._progress = None


__all__ = ["TerminalPresenter"]
