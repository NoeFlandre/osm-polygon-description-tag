from __future__ import annotations

import json
import os
import sys
from collections import defaultdict
from pathlib import Path
from types import SimpleNamespace
from typing import ClassVar

import pytest

from scripts import check_mutation_score, run_mutation_gate
from scripts.mutation_scratch import bounded_pytest_runner
from scripts.run_mutation_gate import (
    clean_test_selection,
    escalation_stages,
    mutated_function_names,
    mutation_batches,
    parse_changed_lines,
    with_probe_canary,
)

PROJECT_ROOT = Path(__file__).resolve().parents[2]


def test_the_forced_fail_probe_follows_the_runs_test_selection(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A narrowed run must probe with tests that reach the code it mutates.

    The probe proves the harness can still observe a failure. A fixed smoke
    file cannot fail for a scope it never imports, which surfaces as
    ``Unable to force test failures`` and looks like a broken harness rather
    than an out-of-scope probe. The probe runs with the selection in effect
    when mutmut forces the failure, and the runner's own selection is restored.
    """
    import mutmut.__main__ as mutmut_main

    probed: list[list[str]] = []
    monkeypatch.setattr(
        mutmut_main,
        "run_forced_fail_test",
        lambda runner: probed.append(list(runner._pytest_add_cli_args_test_selection)),
    )
    runner = SimpleNamespace(_pytest_add_cli_args_test_selection=["tests/unit/original.py"])
    selection = ["tests/unit/dataset/languages/test_detector.py"]

    run_mutation_gate._verify_mutmut_can_fail(runner, selection)
    run_mutation_gate._verify_mutmut_can_fail(runner, ())

    assert probed == [selection, list(run_mutation_gate.SMOKE_TEST_SELECTION)]
    assert runner._pytest_add_cli_args_test_selection == ["tests/unit/original.py"]


def test_the_whole_repository_probe_runs_tests_that_exist_and_reach_the_canary() -> None:
    """Every shard mutates the dataset text canary, so the smoke tests must import it."""
    for selected in run_mutation_gate.SMOKE_TEST_SELECTION:
        smoke = PROJECT_ROOT / selected
        assert smoke.is_file(), selected
        assert "osm_polygon_description_tag.dataset.text" in smoke.read_text(encoding="utf-8")


def test_sharded_mutation_gate_keeps_the_full_strictness() -> None:
    """Sharding may split which modules are mutated, never how strict the gate is."""
    justfile = (PROJECT_ROOT / "justfile").read_text(encoding="utf-8")
    workflow = (PROJECT_ROOT / ".github" / "workflows" / "quality.yml").read_text(encoding="utf-8")
    shard_recipe = justfile.split("mutation-shard scope_file mutate_file:", 1)[1].split("\n\n", 1)[
        0
    ]

    assert "--only-mutate-file" in shard_recipe
    assert "--minimum-score 100" in shard_recipe
    assert "--changed-lines-file" not in shard_recipe
    # Mutation runs over the superset that carries the probe's canary; scoring
    # stays on the shard's own files so the shards remain a clean partition.
    assert '--only-mutate-file "{{mutate_file}}"' in shard_recipe
    assert '--scope-file "{{scope_file}}"' in shard_recipe

    shard_count = int(workflow.split('MUTATION_SHARD_COUNT: "', 1)[1].split('"', 1)[0])
    matrix = workflow.split("shard: [", 1)[1].split("]", 1)[0]
    assert len([entry for entry in matrix.split(",") if entry.strip()]) == shard_count


def test_scoped_mutation_selects_tests_from_the_coverage_map() -> None:
    """PR mutation selects by what covers the function, not by what the branch touched.

    This reverses an earlier rule that kept the map out of the scoped gate to
    protect the timeout. Measured on a pull request changing 69 test files and
    303 source functions, the file-based rule ran 2,047 tests per mutant --
    620,241 test-executions for one mutant each -- against 20,745 from the map,
    which costs 2m37s to record. It is also the sounder rule: a mutant killable
    only by a test the branch did not touch survives the file-based one.
    """
    justfile = (PROJECT_ROOT / "justfile").read_text(encoding="utf-8")
    header, _, body = justfile.partition("mutation-scope scope_file:")
    scope_recipe = body.split("\n\n", 1)[0]
    del header

    # The map has to be recorded before the recipe reads it.
    assert scope_recipe.splitlines()[0].strip() == "mutation-contexts"
    assert "--coverage-file" in scope_recipe
    assert "--changed-lines-file" in scope_recipe
    assert "--test-selection-file" not in scope_recipe


def test_mutation_scope_parser_keeps_only_added_or_modified_new_lines() -> None:
    diff = """diff --git a/src/example.py b/src/example.py
index 1111111..2222222 100644
--- a/src/example.py
+++ b/src/example.py
@@ -4,2 +4,4 @@ def existing():
 old
+new
+also_new
@@ -20,0 +22,2 @@ def added():
+return 1
+return 2
"""

    assert parse_changed_lines(diff) == {
        "src/example.py": (5, 6, 22, 23),
    }


DELETION_ONLY_DIFF = """diff --git a/src/example.py b/src/example.py
index 1111111..2222222 100644
--- a/src/example.py
+++ b/src/example.py
@@ -3,2 +2,0 @@ def existing():
-old_one
-old_two
@@ -10 +7,0 @@
-gone
"""


MIXED_DIFF = """diff --git a/src/example.py b/src/example.py
index 1111111..2222222 100644
--- a/src/example.py
+++ b/src/example.py
@@ -3,2 +2,0 @@ def existing():
-old_one
-old_two
@@ -10,0 +8,2 @@
+new_one
+new_two
"""


def test_mutation_scope_keeps_added_lines_of_a_mixed_diff_and_skips_deletions() -> None:
    assert parse_changed_lines(MIXED_DIFF) == {"src/example.py": (8, 9)}


def _run_scope_check(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], scope_file: Path
) -> tuple[str, str]:
    """Run the scope check and return its (stdout, stderr); it must never run the gate."""
    monkeypatch.setattr(
        run_mutation_gate,
        "run_gate",
        lambda **_kwargs: pytest.fail("the scope check must not run the gate"),
    )
    monkeypatch.setattr(
        sys,
        "argv",
        ["run_mutation_gate", "--check-changed-scope", str(scope_file)],
    )

    run_mutation_gate.main()

    captured = capsys.readouterr()
    return captured.out, captured.err


def test_scope_check_passes_a_deletion_only_diff_as_nothing_to_mutate(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], tmp_path: Path
) -> None:
    scope_file = tmp_path / "mutation-scope.diff"
    scope_file.write_text(DELETION_ONLY_DIFF, encoding="utf-8")

    stdout, stderr = _run_scope_check(monkeypatch, capsys, scope_file)

    assert stdout == "false\n"
    assert "nothing to mutate" in stderr


def test_scope_check_passes_an_empty_diff_as_nothing_to_mutate(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], tmp_path: Path
) -> None:
    scope_file = tmp_path / "mutation-scope.diff"
    scope_file.write_text("", encoding="utf-8")

    stdout, _stderr = _run_scope_check(monkeypatch, capsys, scope_file)

    assert stdout == "false\n"


def test_scope_check_sends_a_mixed_diff_to_the_gate(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], tmp_path: Path
) -> None:
    scope_file = tmp_path / "mutation-scope.diff"
    scope_file.write_text(MIXED_DIFF, encoding="utf-8")

    stdout, _stderr = _run_scope_check(monkeypatch, capsys, scope_file)

    assert stdout == "true\n"


def test_direct_gate_run_still_rejects_a_deletion_only_diff(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    scope_file = tmp_path / "mutation-scope.diff"
    scope_file.write_text(DELETION_ONLY_DIFF, encoding="utf-8")
    monkeypatch.setattr(
        run_mutation_gate,
        "run_gate",
        lambda **_kwargs: pytest.fail("a deletion-only diff must not reach the gate"),
    )
    monkeypatch.setattr(
        sys,
        "argv",
        ["run_mutation_gate", "--changed-lines-file", str(scope_file)],
    )

    with pytest.raises(SystemExit, match="contains no Python source changes"):
        run_mutation_gate.main()


def test_the_scope_step_asks_the_gate_parser_before_running_the_gate() -> None:
    """A non-empty diff is not a reason to run the gate; the parser decides."""
    workflow = (PROJECT_ROOT / ".github" / "workflows" / "quality.yml").read_text(encoding="utf-8")

    assert "--check-changed-scope" in workflow
    assert '[ -s "$RUNNER_TEMP/mutation-scope.txt" ]' not in workflow
    assert "steps.scope.outputs.changed == 'true'" in workflow


def test_mutated_function_names_are_read_from_generated_metadata(tmp_path: Path) -> None:
    metadata_path = tmp_path / "mutants" / "src" / "example.py.meta"
    metadata_path.parent.mkdir(parents=True)
    metadata_path.write_text(
        json.dumps(
            {
                "exit_code_by_key": {
                    "pkg.example.x_function__mutmut_1": None,
                    "pkg.example.x_function__mutmut_2": 1,
                    "pkg.example.x_other__mutmut_1": 0,
                }
            }
        ),
        encoding="utf-8",
    )

    assert mutated_function_names(tmp_path / "mutants") == {
        "pkg.example.x_function",
        "pkg.example.x_other",
    }


def test_unresolved_mutants_lists_every_non_killed_result(monkeypatch, tmp_path: Path) -> None:
    import scripts.run_mutation_gate as gate

    monkeypatch.setattr(
        gate,
        "iter_mutant_exit_codes",
        lambda _root: [
            ("pkg.mod.x_killed__mutmut_1", 1),
            ("pkg.mod.x_survived__mutmut_1", 0),
            ("pkg.mod.x_unchecked__mutmut_1", None),
            ("pkg.mod.x_unknown__mutmut_1", 999),
        ],
    )

    assert gate.unresolved_mutants(tmp_path) == [
        "pkg.mod.x_survived__mutmut_1",
        "pkg.mod.x_unchecked__mutmut_1",
        "pkg.mod.x_unknown__mutmut_1",
    ]


def test_mutation_scope_keeps_recorded_map_without_a_scope_or_generated_names(
    monkeypatch,
) -> None:
    import scripts.run_mutation_gate as gate

    associations = {"pkg.mod.x_function": ("tests/test_mod.py::test_function",)}
    assert gate._scope_associations(associations, None) == associations

    monkeypatch.setattr(gate, "mutated_function_names", lambda _root: set())
    assert gate._scope_associations(associations, {"src/example.py": (2,)}) == associations


def test_runner_options_reject_non_positive_limits() -> None:
    import argparse

    import scripts.run_mutation_gate as gate

    with pytest.raises(SystemExit, match="--max-children must be positive"):
        gate._validate_runner_options(argparse.Namespace(max_children=0, mutation_batch_size=1))
    with pytest.raises(SystemExit, match="--mutation-batch-size must be positive"):
        gate._validate_runner_options(argparse.Namespace(max_children=1, mutation_batch_size=0))


def test_mutation_gate_resets_state_before_first_escalation(monkeypatch) -> None:
    import mutmut
    import mutmut.__main__ as mutmut_main

    import scripts.run_mutation_gate as gate

    events: list[str] = []
    received_scope: list[str] = []
    received_tests: list[str] = []

    class FakeRunner:
        def run_tests(self, *, mutant_name, tests) -> int:
            assert mutant_name is None
            assert tests == ("tests/test_one.py::test_one",)
            events.append("clean")
            return 0

    stats = {
        "duration_by_test": {"tests/test_one.py::test_one": 0.1},
        "function_hashes": {"pkg.mod.x_function": "hash"},
    }
    monkeypatch.setattr(
        gate,
        "_prepare_mutmut",
        lambda _max_children, *, only_mutate=(), test_selection=(), changed_lines=(): (
            received_scope.extend(only_mutate)
            or received_tests.extend(test_selection)
            or FakeRunner()
        ),
    )
    monkeypatch.setattr(gate, "_verify_mutmut_can_fail", lambda *_args: None)
    monkeypatch.setattr(
        gate,
        "recorded_associations",
        lambda _stats, _path: {"pkg.mod.x_function": ("tests/test_one.py::test_one",)},
    )
    monkeypatch.setattr(gate, "coverage_selection", lambda _path: {})
    monkeypatch.setattr(gate, "escalation_stages", lambda _budget: (1,))
    monkeypatch.setattr(gate, "mutated_function_names", lambda _root: {"pkg.mod.x_function"})
    monkeypatch.setattr(gate, "_read_stats", lambda: stats)
    monkeypatch.setattr(gate, "_write_stats", lambda _stats: events.append("write"))
    monkeypatch.setattr(gate, "unresolved_mutants", lambda _root: ["mutant"])
    monkeypatch.setattr(mutmut, "_reset_globals", lambda: events.append("reset"))
    monkeypatch.setattr(
        mutmut, "tests_by_mangled_function_name", {"stale": {"tests/old.py::test_old"}}
    )
    monkeypatch.setattr(
        mutmut, "duration_by_test", defaultdict(float, {"tests/old.py::test_old": 9.0})
    )

    def run_stage(_names, _children) -> None:
        events.append("run")
        mutmut_main.collect_or_load_stats(None)
        assert dict(mutmut.tests_by_mangled_function_name) == {
            "pkg.mod.x_function": {"tests/test_one.py::test_one"}
        }
        assert mutmut.duration_by_test == {"tests/test_one.py::test_one": 0.1}

    monkeypatch.setattr(mutmut_main, "_run", run_stage)
    monkeypatch.setattr(
        mutmut_main,
        "collect_or_load_stats",
        lambda *_args, **_kwargs: events.append("collect-stats"),
    )

    gate.run_gate(
        max_children=1,
        fast_tests_per_function=1,
        only_mutate=("src/osm_polygon_description_tag/dataset/stats.py",),
        test_selection=("tests/unit/publication/test_release.py",),
        changed_lines={"src/osm_polygon_description_tag/dataset/stats.py": (1, 2)},
    )

    assert events == ["clean", "write", "reset", "run"]
    assert received_scope == ["src/osm_polygon_description_tag/dataset/stats.py"]
    assert received_tests == ["tests/unit/publication/test_release.py"]


def test_mutation_gate_defaults_to_single_test_triage() -> None:
    assert escalation_stages() == (1, 8, None)


def test_mutation_batches_are_bounded_and_lossless() -> None:
    mutants = ("first__mutmut_1", "second__mutmut_2", "third__mutmut_3")

    assert mutation_batches(mutants, batch_size=2) == (
        ("first__mutmut_1", "second__mutmut_2"),
        ("third__mutmut_3",),
    )

    assert mutation_batches(range(9)) == (tuple(range(9)),)


def test_clean_test_selection_is_the_sorted_union_of_associations() -> None:
    assert clean_test_selection(
        {
            "pkg.first": ("tests/test_b", "tests/test_a"),
            "pkg.second": ("tests/test_a", "tests/test_c"),
        }
    ) == ("tests/test_a", "tests/test_b", "tests/test_c")


def test_bounded_mutation_runner_cleans_worker_scratch_after_success(tmp_path: Path) -> None:
    """A worker gets one isolated temp root which is removed after its run."""

    class FakeRunner:
        def __init__(self) -> None:
            self._pytest_add_cli_args: list[str] = []

        def run_tests(self, *, mutant_name: str | None, tests: object) -> int:
            assert mutant_name == "mutant"
            assert tests == ()
            worker_root = Path(os.environ["TMPDIR"])
            assert worker_root.is_dir()
            assert f"--basetemp={worker_root}" in self._pytest_add_cli_args
            (worker_root / "created-by-test").write_text("x", encoding="utf-8")
            return 0

    runner = bounded_pytest_runner(FakeRunner, tmp_path)
    assert runner().run_tests(mutant_name="mutant", tests=()) == 0

    assert not (tmp_path / str(os.getpid())).exists()


def test_bounded_mutation_runner_restores_environment_after_failure(tmp_path: Path) -> None:
    """A failed worker cannot leak its temp root or its parent's TMPDIR."""

    original_tmpdir = os.environ.get("TMPDIR")

    class FakeRunner:
        def __init__(self) -> None:
            self._pytest_add_cli_args: list[str] = []

        def run_tests(self, *, mutant_name: str | None, tests: object) -> int:
            assert mutant_name == "mutant"
            raise RuntimeError("test failure")

    runner = bounded_pytest_runner(FakeRunner, tmp_path)()
    try:
        runner.run_tests(mutant_name="mutant", tests=())
    except RuntimeError as error:
        assert str(error) == "test failure"
    else:
        raise AssertionError("the fake runner must fail")

    assert os.environ.get("TMPDIR") == original_tmpdir
    assert runner._pytest_add_cli_args == []
    assert not (tmp_path / str(os.getpid())).exists()


def test_bounded_mutation_runner_can_skip_repeated_clean_runs(tmp_path: Path) -> None:
    """A successful gate preflight makes mutmut's per-batch clean run redundant."""

    class FakeRunner:
        def __init__(self) -> None:
            self._pytest_add_cli_args: list[str] = []

        def run_tests(self, *, mutant_name: str | None, tests: object) -> int:
            raise AssertionError(f"clean run was not skipped: {mutant_name=}, {tests=}")

    runner = bounded_pytest_runner(FakeRunner, tmp_path, skip_clean_tests=True)

    assert runner().run_tests(mutant_name=None, tests=()) == 0


def test_the_mutation_batch_size_is_tunable_and_still_lossless() -> None:
    """Batch size is a speed dial, not a correctness one.

    Every batch costs one mutmut invocation, and each invocation re-scans the
    whole source tree before running anything. At the default of four that
    overhead dominated a full gate. Raising it must not drop or duplicate a
    single mutant.
    """
    mutants = tuple(range(1000))

    batches = mutation_batches(mutants, batch_size=250)

    assert len(batches) == 4
    assert tuple(item for batch in batches for item in batch) == mutants


@pytest.mark.parametrize("batch_size", [1, 7, 999, 5000])
def test_any_positive_batch_size_preserves_every_mutant_in_order(batch_size: int) -> None:
    mutants = tuple(range(1000))

    batches = mutation_batches(mutants, batch_size=batch_size)

    assert tuple(item for batch in batches for item in batch) == mutants
    assert max(map(len, batches)) <= batch_size


def test_the_gate_accepts_a_mutation_batch_size_from_the_command_line() -> None:
    """The operator has to be able to raise it without editing the script."""
    parsed = run_mutation_gate._parse_args_from(["--mutation-batch-size", "250"])

    assert parsed.mutation_batch_size == 250


def test_the_mutation_batch_size_defaults_to_the_module_constant() -> None:
    parsed = run_mutation_gate._parse_args_from([])

    assert parsed.mutation_batch_size == run_mutation_gate.DEFAULT_MUTATION_BATCH_SIZE


def test_the_default_mutation_batch_is_one_lossless_invocation() -> None:
    mutants = tuple(range(1000))

    assert mutation_batches(mutants) == (mutants,)


def test_the_mutation_gate_accepts_a_source_scope_file() -> None:
    parsed = run_mutation_gate._parse_args_from(
        ["--only-mutate-file", "/private/tmp/mutation-scope.txt"]
    )

    assert parsed.only_mutate_file == Path("/private/tmp/mutation-scope.txt")


def test_the_mutation_gate_accepts_a_changed_lines_file() -> None:
    parsed = run_mutation_gate._parse_args_from(
        ["--changed-lines-file", "/private/tmp/mutation-scope.diff"]
    )

    assert parsed.changed_lines_file == Path("/private/tmp/mutation-scope.diff")


def test_changed_line_scope_is_installed_after_mutmut_global_reset(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import mutmut
    from mutmut.configuration import Config

    class FakeConfig:
        only_mutate: ClassVar[list[str]] = []
        pytest_add_cli_args_test_selection: ClassVar[list[str]] = []

    config = FakeConfig()
    monkeypatch.setattr(Config, "ensure_loaded", lambda: None)
    monkeypatch.setattr(Config, "get", lambda: config)
    monkeypatch.setattr(mutmut, "_covered_lines", None)

    run_mutation_gate._configure_mutmut(
        mutmut,
        only_mutate=("src/example.py",),
        test_selection=("tests/test_example.py",),
        changed_lines={"src/example.py": (5, 6)},
    )

    assert config.only_mutate == ["src/example.py"]
    assert config.pytest_add_cli_args_test_selection == ["tests/test_example.py"]
    assert mutmut._covered_lines == {str((Path("mutants") / "src/example.py").absolute()): {5, 6}}


def test_the_mutation_gate_accepts_repeated_source_scope_paths() -> None:
    parsed = run_mutation_gate._parse_args_from(
        [
            "--only-mutate",
            "src/osm_polygon_description_tag/dataset/stats.py",
            "--only-mutate",
            "src/osm_polygon_description_tag/publication/release.py",
        ]
    )

    assert parsed.only_mutate == [
        "src/osm_polygon_description_tag/dataset/stats.py",
        "src/osm_polygon_description_tag/publication/release.py",
    ]


def test_the_mutation_gate_accepts_repeated_test_selection_paths() -> None:
    parsed = run_mutation_gate._parse_args_from(
        [
            "--test-selection",
            "tests/unit/publication/test_release.py",
            "--test-selection",
            "tests/unit/publication/test_upload_helpers.py",
        ]
    )

    assert parsed.test_selection == [
        "tests/unit/publication/test_release.py",
        "tests/unit/publication/test_upload_helpers.py",
    ]


def test_the_mutation_gate_reads_source_scope_file(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    scope_file = tmp_path / "mutation-scope.txt"
    scope_file.write_text(
        "src/osm_polygon_description_tag/dataset/stats.py\n\n"
        "src/osm_polygon_description_tag/publication/release.py\n",
        encoding="utf-8",
    )
    captured: dict[str, object] = {}

    monkeypatch.setattr(
        run_mutation_gate,
        "run_gate",
        lambda **kwargs: captured.update(kwargs),
    )
    monkeypatch.setattr(
        sys,
        "argv",
        ["run_mutation_gate", "--only-mutate-file", str(scope_file)],
    )

    run_mutation_gate.main()

    assert captured["only_mutate"] == (
        "src/osm_polygon_description_tag/dataset/stats.py",
        "src/osm_polygon_description_tag/publication/release.py",
    )


def test_the_mutation_gate_reads_changed_lines_file(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    scope_file = tmp_path / "mutation-scope.diff"
    scope_file.write_text(
        "diff --git a/src/example.py b/src/example.py\n@@ -1,0 +2,2 @@\n+new\n+also_new\n",
        encoding="utf-8",
    )
    captured: dict[str, object] = {}

    monkeypatch.setattr(
        run_mutation_gate,
        "run_gate",
        lambda **kwargs: captured.update(kwargs),
    )
    monkeypatch.setattr(
        sys,
        "argv",
        ["run_mutation_gate", "--changed-lines-file", str(scope_file)],
    )

    run_mutation_gate.main()

    canary = run_mutation_gate.PROBE_CANARY
    canary_lines = len((PROJECT_ROOT / canary).read_text(encoding="utf-8").splitlines())
    assert captured["changed_lines"] == {
        "src/example.py": (2, 3),
        canary: tuple(range(1, canary_lines + 1)),
    }
    assert captured["only_mutate"] == ("src/example.py", canary)


def test_the_changed_lines_gate_mutates_the_whole_probe_canary(tmp_path: Path) -> None:
    """Without the canary, mutmut finds no test for any mutant and stops early."""
    canary = tmp_path / run_mutation_gate.PROBE_CANARY
    canary.parent.mkdir(parents=True)
    canary.write_text("a = 1\nb = 2\nc = 3\n", encoding="utf-8")

    assert with_probe_canary({"src/example.py": (4,)}, tmp_path) == {
        "src/example.py": (4,),
        run_mutation_gate.PROBE_CANARY: (1, 2, 3),
    }


def test_a_changed_canary_keeps_only_its_changed_lines(tmp_path: Path) -> None:
    scoped = {run_mutation_gate.PROBE_CANARY: (7,)}

    assert with_probe_canary(scoped, tmp_path) == scoped


def test_the_canary_the_gates_share_is_the_one_the_probe_tests_import() -> None:
    workflow = (PROJECT_ROOT / ".github" / "workflows" / "quality.yml").read_text(encoding="utf-8")

    assert f'CANARY = "{run_mutation_gate.PROBE_CANARY}"' in workflow


def test_the_mutation_gate_reads_test_selection_file(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    selection_file = tmp_path / "mutation-tests.txt"
    selection_file.write_text(
        "tests/unit/publication/test_release.py\n\ntests/unit/publication/test_upload_helpers.py\n",
        encoding="utf-8",
    )
    captured: dict[str, object] = {}

    monkeypatch.setattr(
        run_mutation_gate,
        "run_gate",
        lambda **kwargs: captured.update(kwargs),
    )
    monkeypatch.setattr(
        sys,
        "argv",
        ["run_mutation_gate", "--test-selection-file", str(selection_file)],
    )

    run_mutation_gate.main()

    assert captured["test_selection"] == (
        "tests/unit/publication/test_release.py",
        "tests/unit/publication/test_upload_helpers.py",
    )


def test_the_metadata_report_names_every_unresolved_mutant(tmp_path: Path) -> None:
    """A failing gate must say which mutants to kill, not only how many."""
    meta = tmp_path / "src" / "pkg" / "mod.py.meta"
    meta.parent.mkdir(parents=True)
    meta.write_text(
        json.dumps(
            {
                "exit_code_by_key": {
                    "x_mod__mutmut_1": 1,  # killed
                    "x_mod__mutmut_2": 0,  # survived
                    "x_mod__mutmut_3": 0,  # survived
                    "x_mod__mutmut_4": 5,  # no_tests
                }
            }
        ),
        encoding="utf-8",
    )

    report = check_mutation_score.build_metadata_report(tmp_path, [], 100.0)

    assert report["unresolved"]["survived"] == 2
    assert report["unresolved_mutants"]["survived"] == [
        "x_mod__mutmut_2",
        "x_mod__mutmut_3",
    ]
    assert report["unresolved_mutants"]["no_tests"] == ["x_mod__mutmut_4"]


def test_a_fully_killed_report_names_no_unresolved_mutant(tmp_path: Path) -> None:
    meta = tmp_path / "src" / "pkg" / "mod.py.meta"
    meta.parent.mkdir(parents=True)
    meta.write_text(
        json.dumps({"exit_code_by_key": {"x_mod__mutmut_1": 1, "x_mod__mutmut_2": 3}}),
        encoding="utf-8",
    )

    report = check_mutation_score.build_metadata_report(tmp_path, [], 100.0)

    assert report["passed"] is True
    assert report["unresolved_mutants"] == {}


def test_a_mutant_name_resolves_to_its_function_module_and_id() -> None:
    """Names are split from the right: a module path may contain underscores."""
    from scripts.show_mutant import split_mutant_name

    assert split_mutant_name("a.b.c.x_func__mutmut_3") == ("a.b.c", "x_func", "3")
    assert split_mutant_name("a.b.xǁCǁm__mutmut_12") == ("a.b", "xǁCǁm", "12")

    for rejected in ("no_marker_here", "x_func__mutmut_1", "__mutmut_1"):
        with pytest.raises(ValueError, match="not a mutant name"):
            split_mutant_name(rejected)


def test_a_module_resolves_to_a_file_or_its_package_init(tmp_path: Path) -> None:
    """Packages are named by their dotted path, not by ``__init__``."""
    from scripts.show_mutant import module_path

    (tmp_path / "pkg" / "sub").mkdir(parents=True)
    (tmp_path / "pkg" / "__init__.py").write_text("")
    (tmp_path / "pkg" / "sub" / "__init__.py").write_text("")
    (tmp_path / "pkg" / "mod.py").write_text("")

    assert module_path("pkg.mod", source_root=tmp_path) == tmp_path / "pkg" / "mod.py"
    assert module_path("pkg.sub", source_root=tmp_path) == tmp_path / "pkg" / "sub" / "__init__.py"

    with pytest.raises(FileNotFoundError, match="pkg.missing"):
        module_path("pkg.missing", source_root=tmp_path)


def test_the_diff_shows_the_mutation_and_not_the_renamed_definition() -> None:
    """Every mutant renames its def, which would otherwise be the whole diff."""
    from scripts.show_mutant import mutant_diff

    source = (
        "def x_f__mutmut_orig(a):\n    return a + 1\n\ndef x_f__mutmut_1(a):\n    return a - 1\n"
    )

    assert mutant_diff(source, "x_f", "1") == "-    return a + 1\n+    return a - 1"

    identical = "def x_g__mutmut_orig(a):\n    return a\n\ndef x_g__mutmut_1(a):\n    return a\n"
    assert "equivalent mutant" in mutant_diff(identical, "x_g", "1")

    with pytest.raises(KeyError, match="no mutant 9"):
        mutant_diff(source, "x_f", "9")
    with pytest.raises(KeyError, match="no mutants generated"):
        mutant_diff(source, "x_missing", "1")
