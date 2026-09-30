"""Manage disposable pytest storage for concurrent mutation workers."""

from __future__ import annotations

import os
import shutil
import tempfile
import threading
from collections.abc import Iterable
from contextlib import contextmanager
from pathlib import Path
from typing import Any


def bounded_pytest_runner(
    runner_class: type[Any],
    scratch_root: Path | None,
    *,
    skip_clean_tests: bool = False,
) -> type[Any]:
    """Wrap mutmut's runner so each worker writes only inside its own scratch dir."""

    class BoundedPytestRunner(runner_class):
        def run_tests(self, *, mutant_name: str | None, tests: Iterable[str]) -> int:
            if mutant_name is None:
                if skip_clean_tests:
                    return 0
                return super().run_tests(mutant_name=mutant_name, tests=tests)
            if scratch_root is None:
                return super().run_tests(mutant_name=mutant_name, tests=tests)

            worker_root = scratch_root / str(os.getpid())
            worker_root.mkdir(parents=True, exist_ok=True)
            previous_args = self._pytest_add_cli_args
            previous_environment = {
                name: os.environ.get(name) for name in ("TMPDIR", "TMP", "TEMP")
            }
            previous_tempfile_dir = tempfile.tempdir
            self._pytest_add_cli_args = [*previous_args, f"--basetemp={worker_root}"]
            for name in previous_environment:
                os.environ[name] = str(worker_root)
            tempfile.tempdir = None
            try:
                return super().run_tests(mutant_name=mutant_name, tests=tests)
            finally:
                self._pytest_add_cli_args = previous_args
                tempfile.tempdir = previous_tempfile_dir
                for name, value in previous_environment.items():
                    if value is None:
                        os.environ.pop(name, None)
                    else:
                        os.environ[name] = value
                shutil.rmtree(worker_root, ignore_errors=True)

    BoundedPytestRunner.__name__ = f"Bounded{runner_class.__name__}"
    return BoundedPytestRunner


def process_is_alive(pid: int) -> bool:
    """Check a worker PID without inspecting any process other than that PID."""
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def _worker_pid(worker_root: Path) -> int | None:
    if worker_root.is_symlink() or not worker_root.is_dir():
        return None
    try:
        return int(worker_root.name)
    except ValueError:
        return None


def remove_finished_worker_dirs(scratch_root: Path) -> None:
    """Remove only numeric worker directories whose exact process has exited."""
    if not scratch_root.is_dir():
        return
    for worker_root in scratch_root.iterdir():
        pid = _worker_pid(worker_root)
        if pid is not None and not process_is_alive(pid):
            shutil.rmtree(worker_root, ignore_errors=True)


class MutationScratchJanitor:
    """Keep hard-timeout worker directories from accumulating on the SSD."""

    def __init__(self, scratch_root: Path, *, interval_s: float = 1.0) -> None:
        self.scratch_root = scratch_root
        self.interval_s = interval_s
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    def __enter__(self) -> MutationScratchJanitor:
        self.scratch_root.mkdir(parents=True, exist_ok=True)
        remove_finished_worker_dirs(self.scratch_root)
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._thread.start()
        return self

    def __exit__(self, *_: object) -> None:
        self._stop.set()
        if self._thread is not None:
            self._thread.join()
        remove_finished_worker_dirs(self.scratch_root)

    def _run(self) -> None:
        while not self._stop.wait(self.interval_s):
            remove_finished_worker_dirs(self.scratch_root)


@contextmanager
def bounded_runner_patch(
    mutmut_main: Any,
    scratch_root: Path | None,
    *,
    skip_clean_tests: bool = False,
):
    """Temporarily install the per-worker runner while mutmut executes mutants."""
    if scratch_root is None and not skip_clean_tests:
        yield
        return
    original_runner = mutmut_main.PytestRunner
    mutmut_main.PytestRunner = bounded_pytest_runner(
        original_runner, scratch_root, skip_clean_tests=skip_clean_tests
    )
    try:
        yield
    finally:
        mutmut_main.PytestRunner = original_runner
