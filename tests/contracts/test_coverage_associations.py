"""Coverage contexts map only measured Python files under the source root."""

from __future__ import annotations

from pathlib import Path

import coverage
import pytest

from scripts import coverage_associations


def test_build_associations_skips_non_python_and_out_of_root_files(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    source_root = tmp_path / "src"
    module = source_root / "pkg" / "module.py"
    module.parent.mkdir(parents=True)
    module.write_text("def first():\n    return 1\n", encoding="utf-8")
    outside = tmp_path / "outside.py"
    outside.write_text("def ignored():\n    return 2\n", encoding="utf-8")
    data_file = source_root / "data.txt"
    data_file.write_text("not Python", encoding="utf-8")

    class FakeCoverageData:
        def __init__(self, *, basename: str) -> None:
            assert basename == str(tmp_path / ".coverage")

        def read(self) -> None:
            return None

        def measured_files(self) -> set[str]:
            return {str(module), str(outside), str(data_file)}

        def contexts_by_lineno(self, path: str) -> dict[int, list[str]]:
            assert path == str(module)
            return {2: ["tests/test_module.py::test_first|run"]}

    monkeypatch.setattr(coverage, "CoverageData", FakeCoverageData)

    actual = coverage_associations.build_associations(tmp_path / ".coverage", source_root)

    assert actual == {
        "pkg.module.x_first": ("tests/test_module.py::test_first",),
    }
