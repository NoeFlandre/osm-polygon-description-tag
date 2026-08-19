from __future__ import annotations

import json
import subprocess
import sys
import tomllib
from pathlib import Path

from packaging.requirements import Requirement

PROJECT_ROOT = Path(__file__).resolve().parents[2]


def _dev_dependency_names() -> set[str]:
    project = tomllib.loads((PROJECT_ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    return {
        Requirement(requirement).name.lower() for requirement in project["dependency-groups"]["dev"]
    }


def test_quality_tools_are_locked_as_development_dependencies() -> None:
    assert {"radon", "mutmut"} <= _dev_dependency_names()


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


def test_quality_recipes_and_required_mutation_gate_are_publicly_wired() -> None:
    justfile = (PROJECT_ROOT / "justfile").read_text(encoding="utf-8")
    workflow = (PROJECT_ROOT / ".github" / "workflows" / "quality.yml").read_text(encoding="utf-8")

    assert "risk:" in justfile
    assert "mutation:" in justfile
    assert "uv run mutmut run" in justfile
    assert "--max-crap-score 6" in justfile
    assert 'publication/planning.py::*"' in justfile
    assert "planning.x*__mutmut_*" in justfile
    assert "mutation:" in workflow
    assert "run: just risk" in workflow
    assert "run: just mutation" in workflow
    assert "mutation-score" in workflow
    assert "reports/crap.json" in workflow
    assert "actions/upload-artifact" in workflow
