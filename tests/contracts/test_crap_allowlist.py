"""The CRAP allow-list excuses listed offenders only until it expires (#77)."""

from __future__ import annotations

import json
from datetime import date
from pathlib import Path

import pytest

from scripts import quality_metrics
from scripts.quality_metrics import apply_allowlist

PROJECT_ROOT = Path(__file__).resolve().parents[2]
ALLOWLIST = {"expires": "2026-12-31", "functions": ["scripts/a.py::slow", "scripts/a.py::fixed"]}
VIOLATIONS = [("scripts/a.py::slow", 9.0), ("src/pkg/new.py::fresh", 7.0)]


def test_listed_violations_are_excused_and_others_still_fail() -> None:
    remaining, problems = apply_allowlist(VIOLATIONS, ALLOWLIST, today=date(2026, 12, 31))

    assert remaining == [("src/pkg/new.py::fresh", 7.0)]
    assert problems == ["remove from the CRAP allow-list, now within budget: scripts/a.py::fixed"]


def test_an_expired_allowlist_excuses_nothing() -> None:
    remaining, problems = apply_allowlist(VIOLATIONS, ALLOWLIST, today=date(2027, 1, 1))

    assert remaining == VIOLATIONS
    assert problems == ["CRAP allow-list expired on 2026-12-31"]


def test_the_repository_allowlist_only_names_scripts_and_expires() -> None:
    allowlist = json.loads((PROJECT_ROOT / "scripts" / "crap-allowlist.json").read_text())

    assert date.fromisoformat(allowlist["expires"])
    assert allowlist["functions"]
    assert all(name.startswith("scripts/") for name in allowlist["functions"])
    assert "issues/77" in allowlist["issue"]


def test_a_stale_entry_named_expired_is_only_a_warning(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    report = tmp_path / "report.json"
    report.write_text(json.dumps({"functions": []}))
    allowlist = tmp_path / "allow.json"
    allowlist.write_text(
        json.dumps({"expires": "2999-12-31", "functions": ["scripts/cache.py::remove_expired"]})
    )
    argv = ["qm", "check", "--report", str(report), "--max-crap-score", "6"]
    monkeypatch.setattr("sys.argv", [*argv, "--allowlist", str(allowlist)])

    quality_metrics.main()

    out = capsys.readouterr().out
    assert "now within budget: scripts/cache.py::remove_expired" in out
    assert "CRAP budget passed" in out
