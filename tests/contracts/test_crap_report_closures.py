"""The CRAP gate scores closures and reports unmatched coverage (#76)."""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

from scripts import quality_metrics
from scripts.quality_metrics import build_report

PROJECT_ROOT = Path(__file__).resolve().parents[2]


def _covered(percent: float, start_line: int) -> dict[str, object]:
    return {"start_line": start_line, "summary": {"percent_covered": percent}}


def _closure(name: str, lineno: int, complexity: int, closures: list | None = None) -> dict:
    return {
        "type": "function",
        "name": name,
        "lineno": lineno,
        "endline": lineno + 1,
        "complexity": complexity,
        "closures": closures or [],
    }


RADON = {
    "src/example.py": [
        _closure("outer", 1, 1, [_closure("inner", 2, 9, [_closure("deepest", 3, 2)])]),
        {
            "type": "method",
            "classname": "Box",
            "name": "open",
            "lineno": 20,
            "endline": 22,
            "complexity": 2,
            "closures": [_closure("helper", 21, 3)],
        },
        {"type": "class", "name": "Box", "lineno": 19, "complexity": 3, "methods": []},
    ]
}
COVERAGE = {
    "files": {
        "src/example.py": {
            "functions": {
                "outer": _covered(100.0, 1),
                "outer.inner": _covered(100.0, 2),
                "Box.open": _covered(50.0, 20),
                "Box.open.helper": _covered(100.0, 21),
            }
        }
    }
}


def test_closures_and_methods_are_scored_with_qualified_names() -> None:
    report = build_report(COVERAGE, RADON)

    scored = {item["name"]: item for item in report["functions"]}
    assert set(scored) == {
        "outer",
        "outer.inner",
        "outer.inner.deepest",
        "Box.open",
        "Box.open.helper",
    }
    assert scored["outer.inner"]["crap_score"] == 9.0  # 100% covered: CRAP == CC
    assert scored["Box.open"]["coverage_percent"] == 50.0


def test_methods_nested_only_under_their_class_are_scored_once() -> None:
    method = {**RADON["src/example.py"][1], "type": "method"}
    nested_only = {
        "src/example.py": [
            {"type": "class", "name": "Box", "lineno": 19, "complexity": 3, "methods": [method]}
        ]
    }
    both = {"src/example.py": [method, nested_only["src/example.py"][0]]}

    for radon in (nested_only, both):
        names = [item["name"] for item in build_report(COVERAGE, radon)["functions"]]
        assert sorted(names) == ["Box.open", "Box.open.helper"]


def test_unmatched_coverage_is_listed_and_scored_as_uncovered() -> None:
    report = build_report(COVERAGE, RADON)

    assert report["unmatched_coverage"] == ["src/example.py::outer.inner.deepest"]
    deepest = next(i for i in report["functions"] if i["name"] == "outer.inner.deepest")
    assert deepest["coverage_percent"] == 0.0
    assert deepest["crap_score"] == 2**2 + 2


def test_start_line_fallback_matches_a_differently_qualified_coverage_key() -> None:
    coverage = {"files": {"src/example.py": {"functions": {"Other.inner": _covered(80.0, 2)}}}}
    radon = {"src/example.py": [_closure("outer", 1, 1, [_closure("inner", 2, 1)])]}

    report = build_report(coverage, radon)

    inner = next(i for i in report["functions"] if i["name"] == "outer.inner")
    assert inner["coverage_percent"] == 80.0
    assert report["unmatched_coverage"] == ["src/example.py::outer"]


def test_check_output_names_closures_and_unmatched_blocks(tmp_path: Path) -> None:
    report_path = tmp_path / "crap.json"
    report_path.write_text(json.dumps(build_report(COVERAGE, RADON)), encoding="utf-8")

    result = subprocess.run(  # noqa: S603 - executable and arguments are repository-controlled
        [
            sys.executable,
            "scripts/quality_metrics.py",
            "check",
            "--report",
            str(report_path),
            "--max-crap-score",
            "6",
        ],
        cwd=PROJECT_ROOT,
        check=False,
        capture_output=True,
        text=True,
    )

    assert result.returncode == 1
    assert "src/example.py::outer.inner: 9.000000" in result.stdout
    assert "5 functions scored, including closures" in result.stdout
    assert "1 without a coverage.py match" in result.stdout
    assert "  src/example.py::outer.inner.deepest" in result.stdout


def test_crap_command_writes_markdown_when_requested(tmp_path: Path, monkeypatch) -> None:
    coverage_path = tmp_path / "coverage.json"
    radon_path = tmp_path / "radon.json"
    output_path = tmp_path / "crap.json"
    markdown_path = tmp_path / "crap.md"
    coverage_path.write_text(
        json.dumps(
            {
                "files": {
                    "src/example.py": {
                        "functions": {
                            "sample": {
                                "start_line": 10,
                                "summary": {"percent_covered": 100.0},
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
                        "complexity": 3,
                    }
                ]
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "quality_metrics",
            "crap",
            "--coverage-json",
            str(coverage_path),
            "--radon-json",
            str(radon_path),
            "--output",
            str(output_path),
            "--markdown-output",
            str(markdown_path),
        ],
    )

    quality_metrics.main()

    assert markdown_path.read_text(encoding="utf-8").count("sample") == 1
    assert "| src/example.py |" not in markdown_path.read_text(encoding="utf-8")
    assert output_path.is_file()
    assert json.loads(output_path.read_text(encoding="utf-8"))["functions"][0]["name"] == "sample"
