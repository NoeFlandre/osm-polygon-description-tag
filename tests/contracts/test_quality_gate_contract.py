from __future__ import annotations

import json
import os
import subprocess
import sys
import tomllib
from collections import defaultdict
from pathlib import Path
from typing import ClassVar

import pytest
from packaging.requirements import Requirement

from scripts import check_mutation_score, run_mutation_gate
from scripts.coverage_associations import (
    associations_for_file,
    mangled_names,
    module_name_for,
)
from scripts.run_mutation_gate import (
    bounded_pytest_runner,
    clean_test_selection,
    complete_associations,
    coverage_selection,
    escalation_stages,
    mutated_function_names,
    mutation_batches,
    parse_changed_lines,
    recorded_associations,
    trim_associations,
)

PROJECT_ROOT = Path(__file__).resolve().parents[2]


def _dev_dependency_names() -> set[str]:
    project = tomllib.loads((PROJECT_ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    return {
        Requirement(requirement).name.lower() for requirement in project["dependency-groups"]["dev"]
    }


def test_quality_tools_are_locked_as_development_dependencies() -> None:
    assert {"radon", "mutmut"} <= _dev_dependency_names()


def test_local_and_ci_coverage_commands_explicitly_measure_branches() -> None:
    commands = [
        line for line in (PROJECT_ROOT / "justfile").read_text().splitlines() if "--cov=" in line
    ]
    assert commands
    assert all("--cov-branch" in command for command in commands)
    workflow = (PROJECT_ROOT / ".github/workflows/quality.yml").read_text()
    assert "--cov-branch" in workflow


def test_mutation_recipes_do_not_force_tmpdir_inside_the_repository() -> None:
    """Negative project-root tests need the caller's external temporary root."""
    justfile = (PROJECT_ROOT / "justfile").read_text(encoding="utf-8")

    assert 'TMPDIR="$PWD/data-root/.tmp"' not in justfile


def test_crap_report_is_deterministic_and_uses_the_documented_formula(tmp_path: Path) -> None:
    coverage_path = tmp_path / "coverage.json"
    radon_path = tmp_path / "radon.json"
    output_path = tmp_path / "crap.json"
    coverage_path.write_text(
        json.dumps(
            {
                "files": {
                    "src/example.py": {
                        "functions": {
                            "sample": {
                                "start_line": 10,
                                "summary": {"percent_covered": 50.0},
                            }
                        }
                    }
                }
            }
        ),
        encoding="utf-8",
    )
    radon_path.write_text(
        json.dumps(
            {
                "src/example.py": [
                    {
                        "type": "function",
                        "name": "sample",
                        "lineno": 10,
                        "endline": 12,
                        "complexity": 4,
                    }
                ]
            }
        ),
        encoding="utf-8",
    )

    subprocess.run(  # noqa: S603 - executable and arguments are repository-controlled
        [
            sys.executable,
            "scripts/quality_metrics.py",
            "crap",
            "--coverage-json",
            str(coverage_path),
            "--radon-json",
            str(radon_path),
            "--output",
            str(output_path),
        ],
        cwd=PROJECT_ROOT,
        check=True,
    )

    payload = json.loads(output_path.read_text(encoding="utf-8"))
    assert payload["formula"] == "complexity**2*(1-coverage_fraction)**3+complexity"
    assert payload["functions"] == [
        {
            "path": "src/example.py",
            "name": "sample",
            "start_line": 10,
            "end_line": 12,
            "complexity": 4,
            "coverage_percent": 50.0,
            "crap_score": 6.0,
        }
    ]


def test_crap_budget_rejects_scores_at_or_above_the_threshold(tmp_path: Path) -> None:
    report_path = tmp_path / "crap.json"
    report_path.write_text(
        json.dumps(
            {
                "functions": [
                    {
                        "path": "src/example.py",
                        "name": "safe",
                        "crap_score": 5.999999,
                    },
                    {
                        "path": "src/example.py",
                        "name": "unsafe",
                        "crap_score": 6.0,
                    },
                ]
            }
        ),
        encoding="utf-8",
    )

    result = subprocess.run(  # noqa: S603 - executable and arguments are repository-controlled
        [
            sys.executable,
            "scripts/quality_metrics.py",
            "check",
            "--report",
            str(report_path),
            "--max-crap-score",
            "6",
            "--pattern",
            "src/example.py::*",
        ],
        cwd=PROJECT_ROOT,
        check=False,
        capture_output=True,
        text=True,
    )

    assert result.returncode == 1
    assert "src/example.py::unsafe" in result.stdout


def test_mutation_gate_can_filter_function_patterns_from_mutmut_metadata(tmp_path: Path) -> None:
    metadata_path = tmp_path / "mutants" / "src" / "example.py.meta"
    metadata_path.parent.mkdir(parents=True)
    metadata_path.write_text(
        json.dumps(
            {
                "exit_code_by_key": {
                    "pkg.example.x_sample__mutmut_1": 1,
                    "pkg.example.x_sample__mutmut_2": 0,
                    "pkg.example.x_other__mutmut_1": None,
                }
            }
        ),
        encoding="utf-8",
    )
    output_path = tmp_path / "mutation.json"

    result = subprocess.run(  # noqa: S603 - executable and arguments are repository-controlled
        [
            sys.executable,
            "scripts/check_mutation_score.py",
            "--mutants-root",
            str(tmp_path / "mutants"),
            "--pattern",
            "pkg.example.x_sample__mutmut_*",
            "--output",
            str(output_path),
            "--minimum-score",
            "50",
        ],
        cwd=PROJECT_ROOT,
        check=False,
    )
    assert result.returncode == 1

    payload = json.loads(output_path.read_text(encoding="utf-8"))
    assert payload["total"] == 2
    assert payload["mutation_score_percent"] == 50.0
    assert payload["unresolved"]["survived"] == 1
    assert payload["passed"] is False


def test_mutation_gate_checks_all_metadata_when_no_pattern_is_given(tmp_path: Path) -> None:
    metadata_path = tmp_path / "mutants" / "src" / "example.py.meta"
    metadata_path.parent.mkdir(parents=True)
    metadata_path.write_text(
        json.dumps(
            {
                "exit_code_by_key": {
                    "pkg.example.x_sample__mutmut_1": 1,
                    "pkg.example.x_other__mutmut_1": 1,
                }
            }
        ),
        encoding="utf-8",
    )
    output_path = tmp_path / "mutation.json"

    result = subprocess.run(  # noqa: S603 - executable and arguments are repository-controlled
        [
            sys.executable,
            "scripts/check_mutation_score.py",
            "--mutants-root",
            str(tmp_path / "mutants"),
            "--output",
            str(output_path),
            "--minimum-score",
            "100",
        ],
        cwd=PROJECT_ROOT,
        check=False,
    )

    assert result.returncode == 0
    payload = json.loads(output_path.read_text(encoding="utf-8"))
    assert payload["total"] == 2
    assert payload["killed"] == 2
    assert payload["patterns"] == []
    assert payload["passed"] is True


def test_mutation_gate_treats_mutmut_timeout_as_unresolved(tmp_path: Path) -> None:
    metadata_path = tmp_path / "mutants" / "src" / "example.py.meta"
    metadata_path.parent.mkdir(parents=True)
    metadata_path.write_text(
        json.dumps({"exit_code_by_key": {"pkg.example.x_sample__mutmut_1": -24}}),
        encoding="utf-8",
    )
    output_path = tmp_path / "mutation.json"

    result = subprocess.run(  # noqa: S603 - executable and arguments are repository-controlled
        [
            sys.executable,
            "scripts/check_mutation_score.py",
            "--mutants-root",
            str(tmp_path / "mutants"),
            "--output",
            str(output_path),
            "--minimum-score",
            "100",
        ],
        cwd=PROJECT_ROOT,
        check=False,
    )

    assert result.returncode == 1
    payload = json.loads(output_path.read_text(encoding="utf-8"))
    assert payload["killed"] == 0
    assert payload["unresolved"]["timeout"] == 1
    assert payload["passed"] is False


def test_mutation_fast_pass_keeps_shortest_tests_deterministically() -> None:
    associations = {
        "pkg.x_first": {"test/slow", "test/fast", "test/tie"},
        "pkg.x_second": {"test/other"},
    }
    durations = {"test/slow": 3.0, "test/fast": 1.0, "test/tie": 1.0, "test/other": 2.0}

    assert trim_associations(associations, durations, max_tests=2) == {
        "pkg.x_first": ("test/fast", "test/tie"),
        "pkg.x_second": ("test/other",),
    }
    assert associations["pkg.x_first"] == {"test/slow", "test/fast", "test/tie"}


def test_mutation_fast_pass_prioritizes_function_module_tests() -> None:
    associations = {
        "pkg.trackio.x_build_snapshot": {
            "tests/test_other.py::test_fast",
            "tests/test_trackio.py::test_slow",
        }
    }
    durations = {
        "tests/test_other.py::test_fast": 0.1,
        "tests/test_trackio.py::test_slow": 1.0,
    }

    assert trim_associations(associations, durations, max_tests=1)[
        "pkg.trackio.x_build_snapshot"
    ] == ("tests/test_trackio.py::test_slow",)


def test_mutation_associations_add_every_test_of_the_same_module() -> None:
    """A single recorded hit must not hide the module's own killing tests."""
    associations = {
        "pkg.alpha.x_direct": {"tests/test_alpha.py::test_direct"},
        "pkg.alpha.x_sibling": {"tests/test_alpha.py::test_indirect"},
        "pkg.beta.x_other": {"tests/test_beta.py::test_other"},
    }

    assert complete_associations(
        associations,
        {
            "tests/test_alpha.py::test_direct": 1.0,
            "tests/test_alpha.py::test_indirect": 2.0,
            "tests/test_beta.py::test_other": 3.0,
        },
    ) == {
        "pkg.alpha.x_direct": (
            "tests/test_alpha.py::test_direct",
            "tests/test_alpha.py::test_indirect",
        ),
        "pkg.alpha.x_sibling": (
            "tests/test_alpha.py::test_direct",
            "tests/test_alpha.py::test_indirect",
        ),
        "pkg.beta.x_other": ("tests/test_beta.py::test_other",),
    }


def test_mutation_associations_run_everything_for_an_unrecorded_function() -> None:
    """A helper mutmut never recorded entering must not inherit only siblings.

    ``_score`` in the GlotLID adapter records no trampoline hit because it is
    reached through the adapter. Giving it just the tests recorded for its
    sibling functions reported killable mutants as survivors, so a function
    with no recording of its own runs the whole collected suite.
    """
    associations = {
        "pkg.glotlid.x__adapter": {"tests/test_fallback.py::test_adapter"},
        "pkg.glotlid.x__score": set(),
    }
    durations = {
        "tests/test_fallback.py::test_adapter": 1.0,
        "tests/test_fallback.py::test_score_rejects_nan": 2.0,
        "tests/test_elsewhere.py::test_other": 3.0,
    }

    selection = complete_associations(associations, durations)

    assert set(selection["pkg.glotlid.x__score"]) == set(durations)
    assert selection["pkg.glotlid.x__adapter"] == ("tests/test_fallback.py::test_adapter",)


def test_mutation_associations_run_the_likeliest_and_cheapest_tests_first() -> None:
    """``pytest -x`` stops at the first failure, so ordering decides the cost."""
    associations = {
        "pkg.trackio.x_build": {
            "tests/test_other.py::test_slow",
            "tests/test_other.py::test_quick",
            "tests/test_trackio.py::test_focused",
        }
    }
    durations = {
        "tests/test_other.py::test_slow": 9.0,
        "tests/test_other.py::test_quick": 0.1,
        "tests/test_trackio.py::test_focused": 4.0,
    }

    assert complete_associations(associations, durations) == {
        "pkg.trackio.x_build": (
            "tests/test_trackio.py::test_focused",
            "tests/test_other.py::test_quick",
            "tests/test_other.py::test_slow",
        )
    }


def test_coverage_selection_keeps_tests_added_since_the_cached_mutation_run(
    tmp_path: Path, monkeypatch
) -> None:
    """A fresh coverage pass is authoritative even when mutmut lacks its duration."""
    import scripts.coverage_associations as coverage_module

    monkeypatch.setattr(
        coverage_module,
        "build_associations",
        lambda _coverage_file, _source_root: {"pkg.mod.x_function": ("tests/new_test",)},
    )
    coverage_file = tmp_path / ".coverage"
    coverage_file.write_bytes(b"coverage")

    assert coverage_selection(coverage_file, {}) == {"pkg.mod.x_function": ("tests/new_test",)}


def test_mutation_recording_survives_an_interrupted_narrow_pass(tmp_path: Path) -> None:
    stats = {
        "function_hashes": {"pkg.alpha.x_direct": "hash"},
        "tests_by_mangled_function_name": {
            "pkg.alpha.x_direct": ["tests/test_alpha.py::b", "tests/test_alpha.py::a"]
        },
    }
    path = tmp_path / "recorded.json"

    first = recorded_associations(stats, path)

    assert first == {"pkg.alpha.x_direct": ("tests/test_alpha.py::a", "tests/test_alpha.py::b")}
    narrowed = {
        "function_hashes": {"pkg.alpha.x_direct": "hash"},
        "tests_by_mangled_function_name": {"pkg.alpha.x_direct": ["tests/test_alpha.py::a"]},
    }
    assert recorded_associations(narrowed, path) == first


def test_mutation_escalation_grows_the_selection_before_the_exact_pass() -> None:
    stages = escalation_stages(5)

    assert stages == (5, 40, None)
    assert len(set(stages)) == len(stages)


def test_coverage_associations_name_functions_the_way_mutmut_does() -> None:
    """The map is only usable if its keys match mutmut's mangled names."""
    source = """
def top_level():
    return 1


class Holder:
    def method(self):
        return 2

    async def coroutine(self):
        return 3
"""

    spans = mangled_names(source, "pkg.mod")

    assert set(spans) == {
        "pkg.mod.x_top_level",
        "pkg.mod.xǁHolderǁmethod",
        "pkg.mod.xǁHolderǁcoroutine",
    }
    start, end = spans["pkg.mod.x_top_level"]
    assert start <= end


def test_coverage_associations_ignore_function_definition_lines(tmp_path: Path) -> None:
    """Import-time execution of a ``def`` line cannot kill a body mutation."""
    module = tmp_path / "mod.py"
    module.write_text(
        "def first():\n    return 1\n",
        encoding="utf-8",
    )

    associations = associations_for_file(
        module,
        "pkg.mod",
        {1: ["tests/test_import.py::test_import|run"]},
    )

    assert associations["pkg.mod.x_first"] == ()


def test_coverage_associations_keep_only_tests_that_execute_the_function(
    tmp_path: Path,
) -> None:
    """A test that never runs a function's lines cannot kill its mutants."""
    module = tmp_path / "mod.py"
    module.write_text(
        "def first():\n    return 1\n\n\ndef second():\n    return 2\n",
        encoding="utf-8",
    )
    contexts = {
        2: ["tests/test_a.py::test_first|run"],
        6: ["tests/test_b.py::test_second|run", "tests/test_b.py::test_second|setup"],
    }

    associations = associations_for_file(module, "pkg.mod", contexts)

    assert associations["pkg.mod.x_first"] == ("tests/test_a.py::test_first",)
    assert associations["pkg.mod.x_second"] == ("tests/test_b.py::test_second",)


def test_coverage_associations_report_an_uncovered_function_as_having_no_tests(
    tmp_path: Path,
) -> None:
    """An uncovered function must surface as a gap, not inherit a neighbour."""
    module = tmp_path / "mod.py"
    module.write_text("def covered():\n    return 1\n\n\ndef bare():\n    return 2\n", "utf-8")

    associations = associations_for_file(
        module, "pkg.mod", {2: ["tests/test_a.py::test_covered|run"]}
    )

    assert associations["pkg.mod.x_covered"] == ("tests/test_a.py::test_covered",)
    assert associations["pkg.mod.x_bare"] == ()


def test_every_source_function_gets_a_mutmut_shaped_name() -> None:
    """The fast selection is keyed by mutmut's names, so they must all match.

    If mutmut ever changes how it mangles a name, the coverage-derived
    selection silently stops applying to that function and the gate quietly
    falls back to running far more tests, so pin the shape here.
    """
    derived: set[str] = set()
    for path in sorted((PROJECT_ROOT / "src").rglob("*.py")):
        module = module_name_for(path, PROJECT_ROOT / "src")
        derived |= set(mangled_names(path.read_text(encoding="utf-8"), module))

    assert "osm_polygon_description_tag.workflow.grid_policy.x_parse_usage_policy_json" in derived
    assert "osm_polygon_description_tag.dataset.languages.models.x__cascade_fingerprint" in derived
    assert (
        "osm_polygon_description_tag.publication.language_hub"
        ".xǁHuggingFaceLanguageHubǁ_downloaded_sha256" in derived
    )


def test_coverage_association_module_names_match_the_installed_package() -> None:
    name = module_name_for(
        PROJECT_ROOT / "src/osm_polygon_description_tag/workflow/grid_policy.py",
        PROJECT_ROOT / "src",
    )

    assert name == "osm_polygon_description_tag.workflow.grid_policy"


def test_static_cast_pragmas_are_attached_to_mutatable_statement_lines() -> None:
    """Every documented static cast must be excluded by mutmut itself.

    A pragma on a multiline call argument is not attached to a statement node
    in LibCST, so it does not suppress the equivalent mutation. Keep these
    narrowing-only casts on standalone statements with line-scoped pragmas.
    """
    from libcst import MetadataWrapper, parse_module
    from mutmut.mutation.pragma_handling import get_ignored_lines

    for path in sorted((PROJECT_ROOT / "src").rglob("*.py")):
        source = path.read_text(encoding="utf-8")
        ignored = get_ignored_lines(
            str(path), source, MetadataWrapper(parse_module(source))
        ).no_mutate_lines
        for line_number, line in enumerate(source.splitlines(), 1):
            if "cast(" in line and "# pragma: no mutate" in line:
                assert line_number in ignored, f"unrecognized cast pragma: {path}:{line_number}"


def test_quality_recipes_and_required_mutation_gate_are_publicly_wired() -> None:
    project = tomllib.loads((PROJECT_ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    justfile = (PROJECT_ROOT / "justfile").read_text(encoding="utf-8")
    workflow = (PROJECT_ROOT / ".github" / "workflows" / "quality.yml").read_text(encoding="utf-8")

    assert "risk:" in justfile
    assert "mutation:" in justfile
    assert "mutation-scope" in justfile
    assert "mutation-shard" in justfile
    assert "run_mutation_gate" in justfile
    assert "uv run python -m scripts.run_mutation_gate" in justfile
    assert "--changed-lines-file" in justfile
    assert "--test-selection-file" in justfile
    assert "--max-crap-score 6" in justfile
    assert "--pattern" not in justfile
    assert "planning.x*__mutmut_*" not in justfile
    assert "all source modules" in justfile
    assert "branches:" in workflow
    assert "main" in workflow
    assert "github.event_name == 'pull_request'" in workflow
    assert "github.event_name == 'push'" in workflow
    assert "just mutation-scope" in workflow
    assert "--unified=0" in workflow
    assert "scripts/**/*.py" in workflow
    assert "tests/**/*.py" in workflow
    assert "mutation-scope:" in workflow
    assert "mutation-all:" in workflow
    assert "run: just risk" in workflow
    assert "just mutation-shard" in workflow
    assert "mutation-score" in workflow
    assert "reports/crap.json" in workflow
    assert "actions/upload-artifact" in workflow
    assert project["tool"]["mutmut"]["pytest_add_cli_args_test_selection"] == ["tests"]


def _write_meta(root: Path, source: str, exit_codes: dict[str, int]) -> None:
    path = root / f"{source}.meta"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"exit_code_by_key": exit_codes}), encoding="utf-8")


def test_a_shard_scores_only_its_own_sources(tmp_path: Path) -> None:
    """A shard must not be judged on mutants another shard is responsible for."""
    mutants_root = tmp_path / "mutants"
    _write_meta(mutants_root, "src/pkg/mine.py", {"pkg.mine.x_a__mutmut_1": 1})
    # A mutant another shard owns, never executed here, so recorded as survived.
    _write_meta(mutants_root, "src/pkg/theirs.py", {"pkg.theirs.x_b__mutmut_1": 0})

    scope = tmp_path / "shard.txt"
    scope.write_text("src/pkg/mine.py\n", encoding="utf-8")

    scoped = check_mutation_score.build_metadata_report(mutants_root, [], 100.0, scope_file=scope)
    whole_tree = check_mutation_score.build_metadata_report(mutants_root, [], 100.0)

    assert scoped["total"] == 1
    assert scoped["mutation_score_percent"] == 100.0
    assert scoped["passed"] is True
    # Without the scope the other shard's untouched mutant drags the score down.
    assert whole_tree["total"] == 2
    assert whole_tree["passed"] is False


def test_shard_scopes_together_cover_every_mutant(tmp_path: Path) -> None:
    """The shards must partition scoring, so nothing is scored twice or missed."""
    mutants_root = tmp_path / "mutants"
    sources = [f"src/pkg/mod{index}.py" for index in range(5)]
    for index, source in enumerate(sources):
        _write_meta(mutants_root, source, {f"pkg.mod{index}.x_f__mutmut_1": 1})

    shard_totals = 0
    for shard in range(2):
        scope = tmp_path / f"shard-{shard}.txt"
        scope.write_text(
            "".join(f"{source}\n" for i, source in enumerate(sources) if i % 2 == shard),
            encoding="utf-8",
        )
        shard_totals += check_mutation_score.build_metadata_report(
            mutants_root, [], 100.0, scope_file=scope
        )["total"]

    assert (
        shard_totals == check_mutation_score.build_metadata_report(mutants_root, [], 100.0)["total"]
    )


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


def test_scoped_mutation_recipe_does_not_collect_all_test_contexts() -> None:
    """PR mutation must not spend the gate timeout rebuilding the full map."""
    justfile = (PROJECT_ROOT / "justfile").read_text(encoding="utf-8")
    scope_recipe = justfile.split("mutation-scope scope_file test_scope_file:", 1)[1].split(
        "\n\n", 1
    )[0]

    assert "mutation-contexts" not in scope_recipe
    assert "--coverage-file" not in scope_recipe
    assert "--changed-lines-file" in scope_recipe


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
    monkeypatch.setattr(gate, "_verify_mutmut_can_fail", lambda _runner: None)
    monkeypatch.setattr(
        gate,
        "recorded_associations",
        lambda _stats, _path: {"pkg.mod.x_function": ("tests/test_one.py::test_one",)},
    )
    monkeypatch.setattr(gate, "coverage_selection", lambda _path, _durations: {})
    monkeypatch.setattr(gate, "escalation_stages", lambda _budget: (1,))
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
    from scripts.run_mutation_gate import escalation_stages

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

    assert captured["changed_lines"] == {"src/example.py": (2, 3)}


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
