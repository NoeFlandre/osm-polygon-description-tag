"""Worker scratch cleanup never removes live or unrelated directories."""

from __future__ import annotations

from pathlib import Path

import pytest

from scripts import mutation_scratch


def test_process_is_alive_distinguishes_dead_and_unreadable_processes(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(mutation_scratch.os, "kill", lambda _pid, _signal: None)
    assert mutation_scratch.process_is_alive(1)

    def missing(_pid: int, _signal: int) -> None:
        raise ProcessLookupError

    monkeypatch.setattr(mutation_scratch.os, "kill", missing)
    assert not mutation_scratch.process_is_alive(2)

    def unreadable(_pid: int, _signal: int) -> None:
        raise PermissionError

    monkeypatch.setattr(mutation_scratch.os, "kill", unreadable)
    assert mutation_scratch.process_is_alive(3)


def test_worker_pid_accepts_only_numeric_real_directories(tmp_path: Path) -> None:
    numeric = tmp_path / "123"
    numeric.mkdir()
    named = tmp_path / "worker"
    named.mkdir()
    regular_file = tmp_path / "456"
    regular_file.write_text("file", encoding="utf-8")
    target = tmp_path / "target"
    target.mkdir()
    link = tmp_path / "789"
    link.symlink_to(target, target_is_directory=True)

    assert mutation_scratch._worker_pid(numeric) == 123
    assert mutation_scratch._worker_pid(named) is None
    assert mutation_scratch._worker_pid(regular_file) is None
    assert mutation_scratch._worker_pid(link) is None


def test_cleanup_removes_only_dead_numeric_worker_directories(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    live = tmp_path / "101"
    dead = tmp_path / "202"
    unrelated = tmp_path / "notes"
    target = tmp_path / "target"
    for directory in (live, dead, unrelated, target):
        directory.mkdir()
    link = tmp_path / "303"
    link.symlink_to(target, target_is_directory=True)
    monkeypatch.setattr(mutation_scratch, "process_is_alive", lambda pid: pid == 101)

    mutation_scratch.remove_finished_worker_dirs(tmp_path)
    mutation_scratch.remove_finished_worker_dirs(tmp_path / "missing")

    assert live.is_dir()
    assert not dead.exists()
    assert unrelated.is_dir()
    assert link.is_symlink()
    assert target.is_dir()


def test_janitor_context_cleans_before_and_after_its_thread(tmp_path: Path, monkeypatch) -> None:
    cleaned: list[Path] = []

    class FakeThread:
        def __init__(self, *, target, daemon) -> None:
            self.target = target
            self.daemon = daemon
            self.started = False
            self.joined = False

        def start(self) -> None:
            self.started = True

        def join(self) -> None:
            self.joined = True

    monkeypatch.setattr(mutation_scratch.threading, "Thread", FakeThread)
    monkeypatch.setattr(
        mutation_scratch,
        "remove_finished_worker_dirs",
        lambda path: cleaned.append(path),
    )
    janitor = mutation_scratch.MutationScratchJanitor(tmp_path)

    assert janitor.__enter__() is janitor
    worker = janitor._thread
    janitor.__exit__(None, None, None)

    assert worker is not None and worker.started and worker.joined
    assert cleaned == [tmp_path, tmp_path]
    assert janitor._stop.is_set()


def test_janitor_thread_waits_between_cleanup_passes(tmp_path: Path, monkeypatch) -> None:
    waits = iter((False, True))
    cleaned: list[Path] = []

    class Stop:
        def wait(self, interval: float) -> bool:
            assert interval == 0.25
            return next(waits)

    janitor = mutation_scratch.MutationScratchJanitor(tmp_path, interval_s=0.25)
    janitor._stop = Stop()
    monkeypatch.setattr(
        mutation_scratch,
        "remove_finished_worker_dirs",
        lambda path: cleaned.append(path),
    )

    janitor._run()

    assert cleaned == [tmp_path]


def test_bounded_runner_patch_restores_the_original_runner() -> None:
    class Runner:
        pass

    class MutmutMain:
        PytestRunner = Runner

    original = MutmutMain.PytestRunner
    with mutation_scratch.bounded_runner_patch(MutmutMain, None, skip_clean_tests=True):
        assert MutmutMain.PytestRunner is not original
    assert MutmutMain.PytestRunner is original
