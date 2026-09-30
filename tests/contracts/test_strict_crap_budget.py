"""The repository CRAP gate evaluates every measured function without exceptions."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from scripts import quality_metrics

PROJECT_ROOT = Path(__file__).resolve().parents[2]


def test_every_over_budget_function_fails_the_crap_gate(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    report = tmp_path / "report.json"
    report.write_text(
        json.dumps(
            {
                "functions": [
                    {"path": "scripts/tool.py", "name": "old", "crap_score": 6.0},
                    {"path": "src/package.py", "name": "new", "crap_score": 9.0},
                ]
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.setattr(
        "sys.argv",
        ["quality_metrics", "check", "--report", str(report), "--max-crap-score", "6"],
    )

    with pytest.raises(SystemExit, match="1"):
        quality_metrics.main()

    output = capsys.readouterr().out
    assert "scripts/tool.py::old: 6.000000" in output
    assert "src/package.py::new: 9.000000" in output
    assert "excused" not in output


def test_the_quality_gate_has_no_allowlist_configuration() -> None:
    justfile = (PROJECT_ROOT / "justfile").read_text(encoding="utf-8")
    source = (PROJECT_ROOT / "scripts" / "quality_metrics.py").read_text(encoding="utf-8")

    assert "--allowlist" not in justfile
    assert "--allowlist" not in source
    assert not (PROJECT_ROOT / "scripts" / "crap-allowlist.json").exists()
