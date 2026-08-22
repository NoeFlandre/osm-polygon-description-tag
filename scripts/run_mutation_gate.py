"""Run the repository-wide mutation gate with a fast pass and exact confirmation.

Mutmut associates every source function with the tests that execute it.  The fast
pass keeps only the shortest deterministic subset of those already-proven tests;
every mutant that is not killed there is then rerun against the complete original
association.  No mutant is excluded: the second pass is the correctness gate.
"""

from __future__ import annotations

import argparse
import json
import os
from collections.abc import Iterable, Mapping
from pathlib import Path
from typing import Any

from scripts.check_mutation_score import STATUS_BY_EXIT_CODE


def trim_associations(
    associations: Mapping[str, Iterable[str]],
    durations: Mapping[str, float],
    *,
    max_tests: int,
) -> dict[str, tuple[str, ...]]:
    """Select the shortest tests for each function without changing the input."""

    if max_tests < 1:
        raise ValueError("max_tests must be positive")

    def priority(function_name: str, test_name: str) -> tuple[int, float, str]:
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

    return {
        function_name: tuple(
            sorted(
                set(test_names),
                key=lambda test_name: priority(function_name, test_name),
            )[:max_tests]
        )
        for function_name, test_names in sorted(associations.items())
    }


def complete_associations(
    associations: Mapping[str, Iterable[str]],
    all_tests: Iterable[str],
) -> dict[str, tuple[str, ...]]:
    """Give unassociated functions the nearest reliable test selection.

    Mutmut records exact trampoline hits, but helpers reached through patched
    boundaries can be omitted from that map even when tests for the same
    module exercise their behavior.  Reusing tests from the same module keeps
    those mutants observable.  A module with no recorded hits falls back to
    the complete collected test set, so an untested function is reported as a
    survivor rather than silently classified as ``no_tests``.
    """

    normalized = {
        function_name: tuple(sorted(set(test_names)))
        for function_name, test_names in sorted(associations.items())
    }
    module_tests: dict[str, set[str]] = {}
    for function_name, test_names in normalized.items():
        module_name = function_name.partition(".x")[0]
        module_tests.setdefault(module_name, set()).update(test_names)
    complete_test_set = tuple(sorted(set(all_tests)))

    completed: dict[str, tuple[str, ...]] = {}
    for function_name, test_names in normalized.items():
        if test_names:
            completed[function_name] = test_names
            continue
        module_name = function_name.partition(".x")[0]
        module_leaf = module_name.rsplit(".", 1)[-1].lower()
        same_module_tests = {
            test_name for test_name in complete_test_set if module_leaf in test_name.lower()
        }
        completed[function_name] = tuple(
            sorted(module_tests.get(module_name, set()) | same_module_tests) or complete_test_set
        )
    return completed


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
    """Run fast mutation triage, then exact confirmation for every survivor."""

    import mutmut
    import mutmut.__main__ as mutmut_main

    runner = _prepare_mutmut(max_children)
    _verify_mutmut_can_fail(runner)

    stats = _read_stats()
    recorded_associations = stats["tests_by_mangled_function_name"]
    all_function_associations = {
        function_name: recorded_associations.get(function_name, ())
        for function_name in stats["function_hashes"]
    }
    full_associations = complete_associations(
        all_function_associations,
        stats["duration_by_test"],
    )
    fast_associations = trim_associations(
        full_associations,
        stats["duration_by_test"],
        max_tests=fast_tests_per_function,
    )
    _write_stats(_replace_associations(stats, fast_associations))

    original_forced_fail = mutmut_main.run_forced_fail_test
    mutmut_main.run_forced_fail_test = lambda _runner: None
    try:
        fast_names = unresolved_mutants(Path("mutants"))
        if fast_names:
            mutmut_main._run(fast_names, max_children)

        remaining_names = unresolved_mutants(Path("mutants"))
        current_stats = _read_stats()
        _write_stats(_replace_associations(current_stats, full_associations))
        if remaining_names:
            mutmut._reset_globals()
            mutmut_main._run(remaining_names, max_children)
    finally:
        mutmut_main.run_forced_fail_test = original_forced_fail


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--max-children", type=int, default=8)
    parser.add_argument("--fast-tests-per-function", type=int, default=5)
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
