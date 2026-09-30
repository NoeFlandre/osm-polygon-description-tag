"""The mutant inspection command groups source files and reports every bad name."""

from __future__ import annotations

import sys
from pathlib import Path
from types import SimpleNamespace

import mutmut.__main__ as mutmut_main
import pytest

from scripts import show_mutant


def test_cli_prints_the_function_body_diff(tmp_path: Path, monkeypatch, capsys) -> None:
    source_root = tmp_path / "src"
    module = source_root / "pkg" / "mod.py"
    module.parent.mkdir(parents=True)
    module.write_text("source = True\n", encoding="utf-8")
    mutated = (
        "def x_run__mutmut_orig():\n    return True\n\ndef x_run__mutmut_1():\n    return False\n"
    )
    monkeypatch.setattr(
        mutmut_main,
        "mutate_file_contents",
        lambda _path, _source: SimpleNamespace(code=mutated),
    )
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "show_mutant",
            "pkg.mod.x_run__mutmut_1",
            "--source-root",
            str(source_root),
        ],
    )

    show_mutant.main()

    output = capsys.readouterr().out
    assert "=== pkg.mod.x_run__mutmut_1" in output
    assert "-    return True" in output
    assert "+    return False" in output


def test_cli_reports_unknown_mutant_and_continues_other_modules(
    tmp_path: Path, monkeypatch, capsys
) -> None:
    source_root = tmp_path / "src"
    modules = (source_root / "pkg" / "one.py", source_root / "pkg" / "two.py")
    for module in modules:
        module.parent.mkdir(parents=True, exist_ok=True)
        module.write_text("source = True\n", encoding="utf-8")
    calls: list[str] = []
    mutated = (
        "def x_run__mutmut_orig():\n    return True\n\ndef x_run__mutmut_1():\n    return False\n"
    )

    def mutate(path: str, _source: str) -> SimpleNamespace:
        calls.append(path)
        return SimpleNamespace(code=mutated)

    monkeypatch.setattr(mutmut_main, "mutate_file_contents", mutate)
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "show_mutant",
            "pkg.one.x_run__mutmut_2",
            "pkg.two.x_run__mutmut_1",
            "--source-root",
            str(source_root),
        ],
    )

    with pytest.raises(SystemExit, match="1"):
        show_mutant.main()

    output = capsys.readouterr()
    assert "no mutant 2 for x_run" in output.err
    assert "=== pkg.two.x_run__mutmut_1" in output.out
    assert len(calls) == 2
