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
from collections.abc import Iterable, Mapping, Sequence
from contextlib import nullcontext
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Protocol, cast

from scripts.check_mutation_score import STATUS_BY_EXIT_CODE, iter_mutant_exit_codes
from scripts.mutation_scratch import (
    MutationScratchJanitor as _MutationScratchJanitor,
)
from scripts.mutation_scratch import (
    bounded_runner_patch as _bounded_runner_patch,
)
from scripts.report_io import write_json_report

DEFAULT_MAX_CHILDREN = 8
DEFAULT_FAST_TESTS_PER_FUNCTION = 1
DEFAULT_MUTATION_BATCH_SIZE: int | None = None
_ESCALATION_FACTOR = 8
MUTANTS_DIR = Path("mutants")
STATS_PATH = MUTANTS_DIR / "mutmut-stats.json"
RECORDED_PATH = MUTANTS_DIR / "mutmut-recorded-tests.json"


# mutmut only records which tests reach mutated code, and its forced-fail probe
# only observes a failure, when the run also mutates code the probe tests import.
# The sharded gate always mutates this module for that reason; the changed-lines
# gate needs it too, or a pull request that leaves it untouched stops before the
# first mutant with "could not find any test case for any mutant".
PROBE_CANARY = "src/osm_polygon_description_tag/dataset/text.py"


@dataclass(frozen=True)
class GateScope:
    only_mutate: tuple[str, ...]
    test_selection: tuple[str, ...]
    changed_lines: Mapping[str, tuple[int, ...]] | None


class MutmutRunner(Protocol):
    """The slice of mutmut's ``PytestRunner`` this gate drives."""

    _pytest_add_cli_args_test_selection: list[str]

    def run_tests(self, *, mutant_name: str | None, tests: Iterable[str]) -> int: ...


@dataclass(frozen=True)
class MutationRun:
    runner: MutmutRunner
    mutmut: Any
    mutmut_main: Any
    durations: Mapping[str, float]
    associations: Mapping[str, Sequence[str]]


def with_probe_canary(
    changed_lines: Mapping[str, tuple[int, ...]], root: Path = Path()
) -> dict[str, tuple[int, ...]]:
    """Return the changed lines plus every line of the canary module."""

    scoped = dict(changed_lines)
    if PROBE_CANARY not in scoped:
        line_count = len((root / PROBE_CANARY).read_text(encoding="utf-8").splitlines())
        scoped[PROBE_CANARY] = tuple(range(1, line_count + 1))
    return scoped


class _DiffScopeParser:
    """Track file and new-line positions while consuming a unified diff."""

    def __init__(self) -> None:
        self.changed: dict[str, set[int]] = {}
        self.current_path: str | None = None
        self.new_line: int | None = None

    def consume(self, line: str) -> None:
        if line.startswith("diff --git "):
            self._start_file(line)
        elif line.startswith("@@") and self.current_path is not None:
            self._start_hunk(line)
        else:
            self._consume_content(line)

    def _start_file(self, line: str) -> None:
        parts = line.split()
        self.current_path = parts[-1][2:] if parts and parts[-1].startswith("b/") else None
        if self.current_path is not None:
            self.changed.setdefault(self.current_path, set())
        self.new_line = None

    def _start_hunk(self, line: str) -> None:
        hunk = line.split("@@", 2)[1].strip().split()
        new_range = next((part[1:] for part in hunk if part.startswith("+")), "")
        start_text, _, _ = new_range.partition(",")
        self.new_line = int(start_text)

    def _consume_content(self, line: str) -> None:
        if self.current_path is None or self.new_line is None or line.startswith("\\"):
            return
        self._advance_new_line(line, self.current_path, self.new_line)

    def _advance_new_line(self, line: str, current_path: str, new_line: int) -> None:
        if line.startswith("+") and not line.startswith("+++"):
            self.changed[current_path].add(new_line)
            self.new_line = new_line + 1
        elif line.startswith("-") and not line.startswith("---"):
            return
        else:
            self.new_line = new_line + 1

    def result(self) -> dict[str, tuple[int, ...]]:
        return {path: tuple(sorted(lines)) for path, lines in sorted(self.changed.items()) if lines}


def parse_changed_lines(diff: str) -> dict[str, tuple[int, ...]]:
    """Parse added/modified new-file lines from a zero-context git diff."""
    parser = _DiffScopeParser()
    for line in diff.splitlines():
        parser.consume(line)
    return parser.result()


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
            str((MUTANTS_DIR / path).absolute()): set(lines)
            for path, lines in changed_lines.items()
        }


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

    return sorted(
        name
        for name, exit_code in iter_mutant_exit_codes(mutants_root)
        if STATUS_BY_EXIT_CODE.get(exit_code, "suspicious") != "killed"
    )


def mutated_function_names(mutants_root: Path) -> set[str]:
    """Return function names represented by the generated mutant metadata."""

    return {
        mutant_name.rsplit("__mutmut_", 1)[0]
        for mutant_name, _exit_code in iter_mutant_exit_codes(mutants_root)
    }


def coverage_selection(coverage_file: Path) -> dict[str, tuple[str, ...]]:
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
    write_json_report(
        path,
        {name: list(tests) for name, tests in recorded.items()},
        indent=None,
        separators=(",", ":"),
    )
    return recorded


def _read_stats() -> dict[str, Any]:
    return json.loads(STATS_PATH.read_text(encoding="utf-8"))


def _write_stats(stats: Mapping[str, Any]) -> None:
    write_json_report(
        STATS_PATH,
        dict(stats),
        indent=4,
        sort_keys=False,
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
) -> MutmutRunner:
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
    MUTANTS_DIR.mkdir(exist_ok=True)
    mutmut_main.copy_src_dir()
    mutmut_main.copy_also_copy_files()
    mutmut_main.setup_source_paths()
    mutmut_main.create_mutants(max_children)
    runner = mutmut_main.PytestRunner()
    runner.prepare_main_test_run()
    mutmut_main.collect_or_load_stats(runner, apply_config_invalidation=True)
    return runner


SMOKE_TEST_SELECTION = ["tests/unit/dataset/test_text.py"]


def _probe_selection(test_selection: Sequence[str]) -> list[str]:
    """Return the tests the forced-fail probe should run.

    The probe proves the harness can still observe a failure. It therefore has
    to run tests that reach the code under mutation: a fixed smoke file cannot
    fail for a scope it never imports, which reads as a broken harness rather
    than as an out-of-scope probe. A narrowed run probes with its own
    selection; a whole-repository run keeps the small smoke file.
    """
    return list(test_selection) if test_selection else list(SMOKE_TEST_SELECTION)


def _verify_mutmut_can_fail(runner: MutmutRunner, test_selection: Sequence[str] = ()) -> None:
    """Run mutmut's failure probe against tests that reach the mutated code."""

    import mutmut.__main__ as mutmut_main

    original_selection = runner._pytest_add_cli_args_test_selection
    runner._pytest_add_cli_args_test_selection = _probe_selection(test_selection)
    try:
        mutmut_main.run_forced_fail_test(cast(Any, runner))
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
    run = _prepare_mutation_run(
        max_children,
        coverage_file=coverage_file,
        only_mutate=only_mutate,
        test_selection=test_selection,
        changed_lines=changed_lines,
    )
    _execute_mutation_run(
        run,
        max_children=max_children,
        fast_tests_per_function=fast_tests_per_function,
        mutation_batch_size=mutation_batch_size,
        only_mutate=only_mutate,
        test_selection=test_selection,
        changed_lines=changed_lines,
    )


def _prepare_mutation_run(
    max_children: int,
    *,
    coverage_file: Path,
    only_mutate: Sequence[str],
    test_selection: Sequence[str],
    changed_lines: Mapping[str, Sequence[int]] | None,
) -> MutationRun:
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
    associations = complete_associations(recorded_associations(stats, RECORDED_PATH), durations)
    associations = _scope_associations(associations, changed_lines)
    associations = _prefer_coverage_associations(associations, coverage_file)
    return MutationRun(runner, mutmut, mutmut_main, durations, associations)


def _scope_associations(
    associations: Mapping[str, Sequence[str]],
    changed_lines: Mapping[str, Sequence[int]] | None,
) -> Mapping[str, Sequence[str]]:
    if changed_lines is None:
        return associations
    selected_functions = mutated_function_names(MUTANTS_DIR)
    if not selected_functions:
        return associations
    return {
        name: selection for name, selection in associations.items() if name in selected_functions
    }


def _prefer_coverage_associations(
    associations: Mapping[str, Sequence[str]], coverage_file: Path
) -> Mapping[str, Sequence[str]]:
    # If coverage has no answer for a function, its recorded selection stays authoritative.
    covered = coverage_selection(coverage_file)
    if not covered:
        return associations
    return {name: covered.get(name) or selection for name, selection in associations.items()}


def _skip_forced_fail_test(_runner: Any) -> None:
    return None


def _execute_mutation_run(
    run: MutationRun,
    *,
    max_children: int,
    fast_tests_per_function: int,
    mutation_batch_size: int | None,
    only_mutate: Sequence[str],
    test_selection: Sequence[str],
    changed_lines: Mapping[str, Sequence[int]] | None,
) -> None:
    main = run.mutmut_main
    original_forced_fail = main.run_forced_fail_test
    main.run_forced_fail_test = cast(Any, _skip_forced_fail_test)
    try:
        _run_clean_preflight(run)
        _run_mutation_passes(
            run,
            max_children=max_children,
            fast_tests_per_function=fast_tests_per_function,
            mutation_batch_size=mutation_batch_size,
            only_mutate=only_mutate,
            test_selection=test_selection,
            changed_lines=changed_lines,
        )
    finally:
        main.run_forced_fail_test = original_forced_fail


def _run_clean_preflight(run: MutationRun) -> None:
    os.environ["MUTANT_UNDER_TEST"] = ""
    clean_tests = clean_test_selection(run.associations)
    if run.runner.run_tests(mutant_name=None, tests=clean_tests) != 0:
        raise SystemExit("clean mutation preflight failed")


def _run_mutation_passes(
    run: MutationRun,
    *,
    max_children: int,
    fast_tests_per_function: int,
    mutation_batch_size: int | None,
    only_mutate: Sequence[str],
    test_selection: Sequence[str],
    changed_lines: Mapping[str, Sequence[int]] | None,
) -> None:
    scratch_root = _mutation_scratch_root()
    janitor = _MutationScratchJanitor(scratch_root) if scratch_root is not None else nullcontext()
    with janitor, _bounded_runner_patch(run.mutmut_main, scratch_root, skip_clean_tests=True):
        _run_escalation_stages(
            run,
            max_children=max_children,
            fast_tests_per_function=fast_tests_per_function,
            mutation_batch_size=mutation_batch_size,
            only_mutate=only_mutate,
            test_selection=test_selection,
            changed_lines=changed_lines,
        )


def _mutation_scratch_root() -> Path | None:
    scratch_base = os.environ.get("MUTATION_TMP_ROOT")
    return Path(scratch_base) / str(os.getpid()) if scratch_base is not None else None


def _run_escalation_stages(
    run: MutationRun,
    *,
    max_children: int,
    fast_tests_per_function: int,
    mutation_batch_size: int | None,
    only_mutate: Sequence[str],
    test_selection: Sequence[str],
    changed_lines: Mapping[str, Sequence[int]] | None,
) -> None:
    main = run.mutmut_main
    original_collect = main.collect_or_load_stats
    main.collect_or_load_stats = cast(Any, lambda *_args, **_kwargs: None)
    try:
        for max_tests in escalation_stages(fast_tests_per_function):
            selection = _association_stage(run, max_tests)
            _write_stats(_replace_associations(_read_stats(), selection))
            remaining = unresolved_mutants(MUTANTS_DIR)
            if not remaining:
                break
            _run_mutant_batches(
                run,
                remaining,
                selection,
                max_children=max_children,
                mutation_batch_size=mutation_batch_size,
                only_mutate=only_mutate,
                test_selection=test_selection,
                changed_lines=changed_lines,
            )
    finally:
        main.collect_or_load_stats = original_collect


def _association_stage(run: MutationRun, max_tests: int | None) -> Mapping[str, Sequence[str]]:
    if max_tests is None:
        return run.associations
    return trim_associations(run.associations, run.durations, max_tests=max_tests)


def _run_mutant_batches(
    run: MutationRun,
    remaining: Sequence[str],
    selection: Mapping[str, Sequence[str]],
    *,
    max_children: int,
    mutation_batch_size: int | None,
    only_mutate: Sequence[str],
    test_selection: Sequence[str],
    changed_lines: Mapping[str, Sequence[int]] | None,
) -> None:
    for mutant_batch in mutation_batches(remaining, batch_size=mutation_batch_size):
        _run_mutant_batch(
            run,
            mutant_batch,
            selection,
            max_children=max_children,
            only_mutate=only_mutate,
            test_selection=test_selection,
            changed_lines=changed_lines,
        )


def _run_mutant_batch(
    run: MutationRun,
    mutant_batch: Sequence[str],
    selection: Mapping[str, Sequence[str]],
    *,
    max_children: int,
    only_mutate: Sequence[str],
    test_selection: Sequence[str],
    changed_lines: Mapping[str, Sequence[int]] | None,
) -> None:
    # Mutmut resets its process state per batch; restore the gate's exact map after each reset.
    run.mutmut._reset_globals()
    _configure_mutmut(
        run.mutmut,
        only_mutate=only_mutate,
        test_selection=test_selection,
        changed_lines=changed_lines,
    )
    run.mutmut.tests_by_mangled_function_name.clear()
    run.mutmut.tests_by_mangled_function_name.update(
        {name: set(tests) for name, tests in selection.items()}
    )
    run.mutmut.duration_by_test.clear()
    run.mutmut.duration_by_test.update(run.durations)
    run.mutmut_main._run(mutant_batch, max_children)


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


def _validate_runner_options(args: argparse.Namespace) -> None:
    if args.max_children < 1:
        raise SystemExit("--max-children must be positive")
    if args.mutation_batch_size is not None and args.mutation_batch_size < 1:
        raise SystemExit("--mutation-batch-size must be positive")


def _read_text_or_exit(path: Path, description: str) -> str:
    try:
        return path.read_text(encoding="utf-8")
    except OSError as error:
        raise SystemExit(f"cannot read {description}: {error}") from error


def _read_scope_lines(path: Path, description: str) -> tuple[str, ...]:
    lines = _read_text_or_exit(path, description).splitlines()
    return tuple(dict.fromkeys(line.strip() for line in lines if line.strip()))


def _read_changed_scope(path: Path) -> dict[str, tuple[int, ...]]:
    changed_lines = parse_changed_lines(_read_text_or_exit(path, "changed lines file"))
    if not changed_lines:
        raise SystemExit("changed lines file contains no Python source changes")
    return with_probe_canary(changed_lines)


def _scope_from_args(args: argparse.Namespace) -> GateScope:
    only_mutate = tuple(args.only_mutate)
    if args.only_mutate_file is not None:
        only_mutate = _read_scope_lines(args.only_mutate_file, "mutation scope file")
    test_selection = tuple(args.test_selection)
    if args.test_selection_file is not None:
        test_selection = _read_scope_lines(args.test_selection_file, "mutation test selection file")
    changed_lines = None
    if args.changed_lines_file is not None:
        changed_lines = _read_changed_scope(args.changed_lines_file)
        only_mutate = tuple(changed_lines)
    return GateScope(only_mutate, test_selection, changed_lines)


def main() -> None:
    args = _parse_args()
    _validate_runner_options(args)
    scope = _scope_from_args(args)
    run_gate(
        max_children=args.max_children,
        fast_tests_per_function=args.fast_tests_per_function,
        coverage_file=args.coverage_file,
        mutation_batch_size=args.mutation_batch_size,
        only_mutate=scope.only_mutate,
        test_selection=scope.test_selection,
        changed_lines=scope.changed_lines,
    )


if __name__ == "__main__":
    main()
