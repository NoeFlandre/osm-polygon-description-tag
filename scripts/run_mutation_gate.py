"""Run the repository-wide mutation gate with escalating passes and exact confirmation.

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
from collections.abc import Iterable, Mapping
from pathlib import Path
from typing import Any

from scripts.check_mutation_score import STATUS_BY_EXIT_CODE

DEFAULT_MAX_CHILDREN = 8
DEFAULT_FAST_TESTS_PER_FUNCTION = 5
_ESCALATION_FACTOR = 8


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


def _stats_path() -> Path:
    return Path("mutants") / "mutmut-stats.json"


def _recorded_path() -> Path:
    return Path("mutants") / "mutmut-recorded-tests.json"


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


def _prepare_mutmut(max_children: int) -> Any:
    """Generate/load the mutmut cache and collect the current test map."""

    import mutmut
    import mutmut.__main__ as mutmut_main
    from mutmut.configuration import Config

    mutmut._reset_globals()
    os.environ["MUTANT_UNDER_TEST"] = "mutation_generation"
    Config.ensure_loaded()
    Path("mutants").mkdir(exist_ok=True)
    mutmut_main.copy_src_dir()
    mutmut_main.copy_also_copy_files()
    mutmut_main.setup_source_paths()
    mutmut_main.create_mutants(max_children)
    runner = mutmut_main.PytestRunner()
    runner.prepare_main_test_run()
    mutmut_main.collect_or_load_stats(runner, apply_config_invalidation=True)
    return runner


def _verify_mutmut_can_fail(runner: Any) -> None:
    """Run mutmut's failure probe with a deliberately small smoke selection."""

    import mutmut.__main__ as mutmut_main

    original_selection = runner._pytest_add_cli_args_test_selection
    runner._pytest_add_cli_args_test_selection = ["tests/unit/test_mutation_surface.py"]
    try:
        mutmut_main.run_forced_fail_test(runner)
    finally:
        runner._pytest_add_cli_args_test_selection = original_selection


def run_gate(*, max_children: int, fast_tests_per_function: int) -> None:
    """Escalate mutation triage, then confirm every survivor exactly."""

    import mutmut
    import mutmut.__main__ as mutmut_main

    runner = _prepare_mutmut(max_children)
    _verify_mutmut_can_fail(runner)

    stats = _read_stats()
    durations = stats["duration_by_test"]
    full_associations = complete_associations(
        recorded_associations(stats, _recorded_path()), durations
    )

    original_forced_fail = mutmut_main.run_forced_fail_test
    mutmut_main.run_forced_fail_test = lambda _runner: None
    try:
        for index, max_tests in enumerate(escalation_stages(fast_tests_per_function)):
            selection = (
                full_associations
                if max_tests is None
                else trim_associations(full_associations, durations, max_tests=max_tests)
            )
            _write_stats(_replace_associations(_read_stats(), selection))
            remaining = unresolved_mutants(Path("mutants"))
            if not remaining:
                break
            if index:
                mutmut._reset_globals()
            mutmut_main._run(remaining, max_children)
    finally:
        mutmut_main.run_forced_fail_test = original_forced_fail


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--max-children", type=int, default=DEFAULT_MAX_CHILDREN)
    parser.add_argument(
        "--fast-tests-per-function", type=int, default=DEFAULT_FAST_TESTS_PER_FUNCTION
    )
    return parser.parse_args()


def main() -> None:
    args = _parse_args()
    if args.max_children < 1:
        raise SystemExit("--max-children must be positive")
    run_gate(
        max_children=args.max_children,
        fast_tests_per_function=args.fast_tests_per_function,
    )


if __name__ == "__main__":
    main()
