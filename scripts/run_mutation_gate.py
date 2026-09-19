"""Run a deterministic mutation gate with escalating passes and exact confirmation.

Mutmut associates every source function with the tests that execute it, and runs
each mutant under ``pytest -x``.  Two consequences shape this gate:

* Ordering decides the cost of a kill.  Every selection is ordered
  focused-tests-first and then cheapest-first, so a mutant that dies usually
  dies within the first test or two instead of after a whole module's suite.
* Only a survivor has to pay for its complete association.  The gate therefore
  escalates: a small selection first, a wider one next, and finally the exact
  complete association recorded by mutmut.

Already-resolved mutants are read from the candidate's metadata and never rerun,
so an interrupted or repeated run resumes instead of starting over.  No mutant is
excluded: the last pass is the correctness gate.
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import tempfile
import threading
from collections.abc import Iterable, Mapping, Sequence
from contextlib import contextmanager, nullcontext
from pathlib import Path
from typing import Any, cast

from scripts.check_mutation_score import STATUS_BY_EXIT_CODE

DEFAULT_MAX_CHILDREN = 8
DEFAULT_FAST_TESTS_PER_FUNCTION = 1
DEFAULT_MUTATION_BATCH_SIZE: int | None = None
_ESCALATION_FACTOR = 8


def parse_changed_lines(diff: str) -> dict[str, tuple[int, ...]]:
    """Parse added/modified new-file lines from a zero-context git diff."""

    changed: dict[str, set[int]] = {}
    current_path: str | None = None
    new_line: int | None = None
    for line in diff.splitlines():
        if line.startswith("diff --git "):
            parts = line.split()
            current_path = parts[-1][2:] if parts and parts[-1].startswith("b/") else None
            if current_path is not None:
                changed.setdefault(current_path, set())
            new_line = None
            continue
        if line.startswith("@@") and current_path is not None:
            hunk = line.split("@@", 2)[1].strip().split()
            new_range = next((part[1:] for part in hunk if part.startswith("+")), "")
            start_text, _, _ = new_range.partition(",")
            new_line = int(start_text)
            continue
        if current_path is None or new_line is None or line.startswith("\\"):
            continue
        if line.startswith("+") and not line.startswith("+++"):
            changed[current_path].add(new_line)
            new_line += 1
        elif line.startswith("-") and not line.startswith("---"):
            continue
        else:
            new_line += 1
    return {path: tuple(sorted(lines)) for path, lines in sorted(changed.items()) if lines}


def _configure_mutmut(
    mutmut: Any,
    *,
    only_mutate: Sequence[str],
    test_selection: Sequence[str],
    changed_lines: Mapping[str, Sequence[int]] | None,
) -> None:
    """Apply runner-only scope after mutmut resets its process globals."""

    from mutmut.configuration import Config

    Config.ensure_loaded()
    config = Config.get()
    config.only_mutate = list(only_mutate)
    if test_selection:
        config.pytest_add_cli_args_test_selection = list(test_selection)
    if changed_lines is not None:
        mutmut._covered_lines = {
            str((Path("mutants") / path).absolute()): set(lines)
            for path, lines in changed_lines.items()
        }


def bounded_pytest_runner(
    runner_class: type[Any],
    scratch_root: Path | None,
    *,
    skip_clean_tests: bool = False,
) -> type[Any]:
    """Wrap a pytest runner with one disposable directory per worker process."""

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


def _process_is_alive(pid: int) -> bool:
    """Return whether a process exists without inspecting unrelated processes."""

    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def _remove_finished_worker_dirs(scratch_root: Path) -> None:
    """Remove only numeric worker directories whose exact PID has exited."""

    if not scratch_root.is_dir():
        return
    for worker_root in scratch_root.iterdir():
        if worker_root.is_symlink() or not worker_root.is_dir():
            continue
        try:
            pid = int(worker_root.name)
        except ValueError:
            continue
        if not _process_is_alive(pid):
            shutil.rmtree(worker_root, ignore_errors=True)


class _MutationScratchJanitor:
    """Keep abandoned hard-timeout directories from accumulating on the SSD."""

    def __init__(self, scratch_root: Path, *, interval_s: float = 1.0) -> None:
        self.scratch_root = scratch_root
        self.interval_s = interval_s
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    def __enter__(self) -> _MutationScratchJanitor:
        self.scratch_root.mkdir(parents=True, exist_ok=True)
        _remove_finished_worker_dirs(self.scratch_root)
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._thread.start()
        return self

    def __exit__(self, *_: object) -> None:
        self._stop.set()
        if self._thread is not None:
            self._thread.join()
        _remove_finished_worker_dirs(self.scratch_root)

    def _run(self) -> None:
        while not self._stop.wait(self.interval_s):
            _remove_finished_worker_dirs(self.scratch_root)


@contextmanager
def _bounded_runner_patch(
    mutmut_main: Any, scratch_root: Path | None, *, skip_clean_tests: bool = False
):
    """Install the bounded runner only while mutmut executes mutant workers."""

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


def test_priority(
    function_name: str, test_name: str, durations: Mapping[str, float]
) -> tuple[int, float, str]:
    """Rank one test for one function: focused first, then cheapest, then by name."""

    nodeid = test_name.lower()
    module_name = function_name.partition(".x")[0].rsplit(".", 1)[-1].lower()
    function_name_only = function_name.rsplit(".", 1)[-1]
    function_name_only = function_name_only.removeprefix("x__").lower()
    focused = module_name in nodeid or function_name_only in nodeid
    return (
        0 if focused else 1,
        float(durations.get(test_name, float("inf"))),
        test_name,
    )


def order_tests(
    function_name: str, test_names: Iterable[str], durations: Mapping[str, float]
) -> tuple[str, ...]:
    """Order one function's tests so the likeliest, cheapest kill runs first."""

    return tuple(
        sorted(
            set(test_names),
            key=lambda test_name: test_priority(function_name, test_name, durations),
        )
    )


def escalation_stages(
    fast_tests_per_function: int = DEFAULT_FAST_TESTS_PER_FUNCTION,
) -> tuple[int | None, ...]:
    """Return the per-function test budgets to try, ending with the exact pass.

    A wider intermediate budget resolves most of what the fast pass misses
    without paying for every function's complete association, and ``None``
    is the final exact pass that decides the gate.
    """

    if fast_tests_per_function < 1:
        raise ValueError("fast_tests_per_function must be positive")
    return (fast_tests_per_function, fast_tests_per_function * _ESCALATION_FACTOR, None)


def mutation_batches(
    mutant_names: Iterable[str], *, batch_size: int | None = DEFAULT_MUTATION_BATCH_SIZE
) -> tuple[tuple[str, ...], ...]:
    """Split a mutation pass into lossless named batches.

    The default is one invocation because each mutmut invocation regenerates
    the complete configured source tree.  An explicit positive batch size is
    retained for operators who need smaller resumable invocations.
    """

    names = tuple(mutant_names)
    if batch_size is None:
        return (names,) if names else ()
    if batch_size < 1:
        raise ValueError("batch_size must be positive")
    return tuple(names[start : start + batch_size] for start in range(0, len(names), batch_size))


def clean_test_selection(
    associations: Mapping[str, Iterable[str]],
) -> tuple[str, ...]:
    """Return the exact test union needed to preflight a mutation pass."""

    return tuple(sorted({test for tests in associations.values() for test in tests}))


def trim_associations(
    associations: Mapping[str, Iterable[str]],
    durations: Mapping[str, float],
    *,
    max_tests: int,
) -> dict[str, tuple[str, ...]]:
    """Select the shortest tests for each function without changing the input."""

    if max_tests < 1:
        raise ValueError("max_tests must be positive")
    return {
        function_name: order_tests(function_name, test_names, durations)[:max_tests]
        for function_name, test_names in sorted(associations.items())
    }


def complete_associations(
    associations: Mapping[str, Iterable[str]],
    durations: Mapping[str, float],
) -> dict[str, tuple[str, ...]]:
    """Give unassociated functions the nearest reliable test selection.

    Mutmut records exact trampoline hits, but that map is demonstrably
    incomplete: functions reached through a CLI entry point or a patched
    boundary can end up associated with a single unrelated test even though a
    whole module's suite exercises them, which reports killable mutants as
    survivors.  Every function therefore also receives the tests recorded for
    its module and the tests named after it, and a module with no recorded hits
    at all falls back to the complete collected test set, so an untested
    function is reported as a survivor rather than silently classified as
    ``no_tests``.
    """

    normalized = {
        function_name: set(test_names) for function_name, test_names in sorted(associations.items())
    }
    module_tests: dict[str, set[str]] = {}
    for function_name, test_names in normalized.items():
        module_name = function_name.partition(".x")[0]
        module_tests.setdefault(module_name, set()).update(test_names)
    complete_test_set = set(durations)

    return {
        function_name: order_tests(
            function_name,
            _selected_tests(function_name, test_names, module_tests, complete_test_set),
            durations,
        )
        for function_name, test_names in normalized.items()
    }


def _selected_tests(
    function_name: str,
    test_names: set[str],
    module_tests: Mapping[str, set[str]],
    complete_test_set: set[str],
) -> set[str]:
    """Return every test that must run against one function's mutants.

    No recorded hit is not evidence of an untested function: a private helper
    reached through an adapter or a patched boundary records no trampoline hit
    at all, and inheriting only its siblings' tests reports killable mutants as
    survivors. Such a function therefore runs the whole collected suite.
    """

    if not test_names:
        return complete_test_set
    return test_names | _module_neighbourhood(function_name, module_tests, complete_test_set)


def _module_neighbourhood(
    function_name: str, module_tests: Mapping[str, set[str]], complete_test_set: set[str]
) -> set[str]:
    """Return every test that plausibly exercises one function's module."""

    module_name = function_name.partition(".x")[0]
    module_leaf = module_name.rsplit(".", 1)[-1].lower()
    same_module_tests = {
        test_name for test_name in complete_test_set if module_leaf in test_name.lower()
    }
    return module_tests.get(module_name, set()) | same_module_tests


def unresolved_mutants(mutants_root: Path) -> list[str]:
    """Return every mutant whose metadata is not a killed result."""

    names: list[str] = []
    for metadata_path in sorted(mutants_root.glob("src/**/*.py.meta")):
        metadata = json.loads(metadata_path.read_text(encoding="utf-8"))
        for name, exit_code in metadata.get("exit_code_by_key", {}).items():
            if STATUS_BY_EXIT_CODE.get(exit_code, "suspicious") != "killed":
                names.append(name)
    return sorted(names)


def mutated_function_names(mutants_root: Path) -> set[str]:
    """Return function names represented by the generated mutant metadata."""

    names: set[str] = set()
    for metadata_path in sorted(mutants_root.glob("src/**/*.py.meta")):
        metadata = json.loads(metadata_path.read_text(encoding="utf-8"))
        names.update(
            mutant_name.rsplit("__mutmut_", 1)[0]
            for mutant_name in metadata.get("exit_code_by_key", {})
        )
    return names


def _stats_path() -> Path:
    return Path("mutants") / "mutmut-stats.json"


def _recorded_path() -> Path:
    return Path("mutants") / "mutmut-recorded-tests.json"


def coverage_selection(
    coverage_file: Path, durations: Mapping[str, float]
) -> dict[str, tuple[str, ...]]:
    """Return exact per-function associations from per-test coverage contexts.

    Returns an empty mapping when the coverage file is absent, so the gate
    still runs on mutmut's own recording alone.
    """

    if not coverage_file.is_file():
        return {}
    from scripts.coverage_associations import build_associations

    associations = build_associations(coverage_file, Path("src"))
    return {
        # The coverage database comes from the current test run. Keep tests
        # added after mutmut's cached duration map; ``order_tests`` already
        # places their unknown duration after known tests.
        name: tuple(tests)
        for name, tests in associations.items()
    }


def recorded_associations(stats: Mapping[str, Any], path: Path) -> dict[str, tuple[str, ...]]:
    """Return mutmut's own recording, preserved across escalating passes.

    Each pass overwrites the stats file with the selection it wants mutmut to
    run, so the recording has to be kept separately: without it, a run
    interrupted during an early narrow pass would treat that narrow selection
    as the truth and report false survivors when resumed.
    """

    if path.is_file():
        payload = json.loads(path.read_text(encoding="utf-8"))
        return {name: tuple(tests) for name, tests in payload.items()}
    recorded = {
        function_name: tuple(sorted(stats["tests_by_mangled_function_name"].get(function_name, ())))
        for function_name in stats["function_hashes"]
    }
    path.write_text(
        json.dumps(
            {name: list(tests) for name, tests in recorded.items()},
            ensure_ascii=False,
            separators=(",", ":"),
            sort_keys=True,
        )
        + "\n",
        encoding="utf-8",
    )
    return recorded


def _read_stats() -> dict[str, Any]:
    return json.loads(_stats_path().read_text(encoding="utf-8"))


def _write_stats(stats: Mapping[str, Any]) -> None:
    _stats_path().write_text(
        json.dumps(dict(stats), ensure_ascii=False, indent=4) + "\n",
        encoding="utf-8",
    )


def _replace_associations(
    stats: Mapping[str, Any], associations: Mapping[str, Iterable[str]]
) -> dict[str, Any]:
    updated = dict(stats)
    updated["tests_by_mangled_function_name"] = {
        key: list(values) for key, values in sorted(associations.items())
    }
    return updated


def _prepare_mutmut(
    max_children: int,
    *,
    only_mutate: Sequence[str] = (),
    test_selection: Sequence[str] = (),
    changed_lines: Mapping[str, Sequence[int]] | None = None,
) -> Any:
    """Generate/load the mutmut cache and collect the current test map."""

    import mutmut
    import mutmut.__main__ as mutmut_main

    mutmut._reset_globals()
    os.environ["MUTANT_UNDER_TEST"] = "mutation_generation"
    # Mutmut does not expose ``only_mutate`` as a command-line option.  Set
    # the loaded configuration before every generation entry point so a PR
    # scope can be applied without changing the repository-wide default.
    _configure_mutmut(
        mutmut,
        only_mutate=only_mutate,
        test_selection=test_selection,
        changed_lines=changed_lines,
    )
    Path("mutants").mkdir(exist_ok=True)
    mutmut_main.copy_src_dir()
    mutmut_main.copy_also_copy_files()
    mutmut_main.setup_source_paths()
    mutmut_main.create_mutants(max_children)
    runner = mutmut_main.PytestRunner()
    runner.prepare_main_test_run()
    mutmut_main.collect_or_load_stats(runner, apply_config_invalidation=True)
    return runner


SMOKE_TEST_SELECTION = ["tests/unit/test_mutation_surface.py"]


def _probe_selection(test_selection: Sequence[str]) -> list[str]:
    """Return the tests the forced-fail probe should run.

    The probe proves the harness can still observe a failure. It therefore has
    to run tests that reach the code under mutation: a fixed smoke file cannot
    fail for a scope it never imports, which reads as a broken harness rather
    than as an out-of-scope probe. A narrowed run probes with its own
    selection; a whole-repository run keeps the small smoke file.
    """
    return list(test_selection) if test_selection else list(SMOKE_TEST_SELECTION)


def _verify_mutmut_can_fail(runner: Any, test_selection: Sequence[str] = ()) -> None:
    """Run mutmut's failure probe against tests that reach the mutated code."""

    import mutmut.__main__ as mutmut_main

    original_selection = runner._pytest_add_cli_args_test_selection
    runner._pytest_add_cli_args_test_selection = _probe_selection(test_selection)
    try:
        mutmut_main.run_forced_fail_test(runner)
    finally:
        runner._pytest_add_cli_args_test_selection = original_selection


def run_gate(
    *,
    max_children: int,
    fast_tests_per_function: int,
    coverage_file: Path = Path(),
    mutation_batch_size: int | None = DEFAULT_MUTATION_BATCH_SIZE,
    only_mutate: Sequence[str] = (),
    test_selection: Sequence[str] = (),
    changed_lines: Mapping[str, Sequence[int]] | None = None,
) -> None:
    """Escalate mutation triage, then confirm every selected survivor exactly."""

    import mutmut
    import mutmut.__main__ as mutmut_main

    runner = _prepare_mutmut(
        max_children,
        only_mutate=only_mutate,
        test_selection=test_selection,
        changed_lines=changed_lines,
    )
    _verify_mutmut_can_fail(runner, test_selection)

    stats = _read_stats()
    durations = stats["duration_by_test"]
    full_associations = complete_associations(
        recorded_associations(stats, _recorded_path()), durations
    )
    if changed_lines is not None:
        selected_functions = mutated_function_names(Path("mutants"))
        if selected_functions:
            full_associations = {
                name: selection
                for name, selection in full_associations.items()
                if name in selected_functions
            }
    # Prefer the exact covering-test set; keep the recorded selection wherever
    # coverage has nothing to say, because running more tests is always sound.
    covered = coverage_selection(coverage_file, durations)
    if covered:
        full_associations = {
            name: covered.get(name) or selection for name, selection in full_associations.items()
        }

    original_forced_fail = mutmut_main.run_forced_fail_test

    def skip_forced_fail_test(_runner: Any) -> None:
        return None

    mutmut_main.run_forced_fail_test = cast(Any, skip_forced_fail_test)
    scratch_base = os.environ.get("MUTATION_TMP_ROOT")
    scratch_root = Path(scratch_base) / str(os.getpid()) if scratch_base is not None else None
    try:
        os.environ["MUTANT_UNDER_TEST"] = ""
        clean_tests = clean_test_selection(full_associations)
        if runner.run_tests(mutant_name=None, tests=clean_tests) != 0:
            raise SystemExit("clean mutation preflight failed")
        with (
            _MutationScratchJanitor(scratch_root) if scratch_root is not None else nullcontext(),
            _bounded_runner_patch(mutmut_main, scratch_root, skip_clean_tests=True),
        ):
            # ``_prepare_mutmut`` already collected the current associations.
            # Mutmut's private ``_run`` recollects them for every escalation;
            # keep the explicit maps below authoritative and avoid paying for
            # another full pytest invocation at each stage.
            original_collect_or_load_stats = mutmut_main.collect_or_load_stats
            mutmut_main.collect_or_load_stats = cast(Any, lambda *_args, **_kwargs: None)
            try:
                for max_tests in escalation_stages(fast_tests_per_function):
                    selection = (
                        full_associations
                        if max_tests is None
                        else trim_associations(full_associations, durations, max_tests=max_tests)
                    )
                    _write_stats(_replace_associations(_read_stats(), selection))
                    remaining = unresolved_mutants(Path("mutants"))
                    if not remaining:
                        break
                    for mutant_batch in mutation_batches(remaining, batch_size=mutation_batch_size):
                        # Load the just-written selection instead of reusing the prior pass's map.
                        mutmut._reset_globals()
                        _configure_mutmut(
                            mutmut,
                            only_mutate=only_mutate,
                            test_selection=test_selection,
                            changed_lines=changed_lines,
                        )
                        mutmut.tests_by_mangled_function_name.clear()
                        mutmut.tests_by_mangled_function_name.update(
                            {name: set(tests) for name, tests in selection.items()}
                        )
                        mutmut.duration_by_test.clear()
                        mutmut.duration_by_test.update(durations)
                        mutmut_main._run(mutant_batch, max_children)
            finally:
                mutmut_main.collect_or_load_stats = original_collect_or_load_stats
    finally:
        mutmut_main.run_forced_fail_test = cast(Any, original_forced_fail)


def _parse_args() -> argparse.Namespace:
    return _parse_args_from(None)


def _parse_args_from(argv: Sequence[str] | None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--max-children", type=int, default=DEFAULT_MAX_CHILDREN)
    parser.add_argument(
        "--coverage-file",
        type=Path,
        default=Path("data-root/.tmp/.coverage-ctx"),
        help="per-test coverage contexts used for exact test selection",
    )
    parser.add_argument(
        "--fast-tests-per-function", type=int, default=DEFAULT_FAST_TESTS_PER_FUNCTION
    )
    parser.add_argument(
        "--mutation-batch-size",
        type=int,
        default=DEFAULT_MUTATION_BATCH_SIZE,
        help=(
            "mutants per mutmut invocation; omit to run each escalation pass in "
            "one invocation (small batches repeat source generation)"
        ),
    )
    scope = parser.add_mutually_exclusive_group()
    scope.add_argument(
        "--only-mutate",
        action="append",
        default=[],
        metavar="PATH",
        help="source path to mutate; may be repeated, otherwise all source is mutated",
    )
    scope.add_argument(
        "--only-mutate-file",
        type=Path,
        metavar="FILE",
        help="newline-delimited source paths to mutate; an empty file selects all source",
    )
    scope.add_argument(
        "--changed-lines-file",
        type=Path,
        metavar="FILE",
        help="zero-context git diff whose added/modified source lines are mutated",
    )
    tests = parser.add_mutually_exclusive_group()
    tests.add_argument(
        "--test-selection",
        action="append",
        default=[],
        metavar="PATH",
        help="pytest path to select; may be repeated, otherwise the configured test root is used",
    )
    tests.add_argument(
        "--test-selection-file",
        type=Path,
        metavar="FILE",
        help=(
            "newline-delimited pytest paths to select; an empty file uses the configured test root"
        ),
    )
    return parser.parse_args(argv)


def main() -> None:
    args = _parse_args()
    if args.max_children < 1:
        raise SystemExit("--max-children must be positive")
    if args.mutation_batch_size is not None and args.mutation_batch_size < 1:
        raise SystemExit("--mutation-batch-size must be positive")
    only_mutate = tuple(args.only_mutate)
    if args.only_mutate_file is not None:
        try:
            only_mutate = tuple(
                dict.fromkeys(
                    line.strip()
                    for line in args.only_mutate_file.read_text(encoding="utf-8").splitlines()
                    if line.strip()
                )
            )
        except OSError as error:
            raise SystemExit(f"cannot read mutation scope file: {error}") from error
    test_selection = tuple(args.test_selection)
    if args.test_selection_file is not None:
        try:
            test_selection = tuple(
                dict.fromkeys(
                    line.strip()
                    for line in args.test_selection_file.read_text(encoding="utf-8").splitlines()
                    if line.strip()
                )
            )
        except OSError as error:
            raise SystemExit(f"cannot read mutation test selection file: {error}") from error
    changed_lines: dict[str, tuple[int, ...]] | None = None
    if args.changed_lines_file is not None:
        try:
            changed_lines = parse_changed_lines(args.changed_lines_file.read_text(encoding="utf-8"))
        except OSError as error:
            raise SystemExit(f"cannot read changed lines file: {error}") from error
        only_mutate = tuple(changed_lines)
        if not only_mutate:
            raise SystemExit("changed lines file contains no Python source changes")
    run_gate(
        max_children=args.max_children,
        fast_tests_per_function=args.fast_tests_per_function,
        coverage_file=args.coverage_file,
        mutation_batch_size=args.mutation_batch_size,
        only_mutate=only_mutate,
        test_selection=test_selection,
        changed_lines=changed_lines,
    )


if __name__ == "__main__":
    main()
