"""The wheel smoke check must catch dropped data files and broken imports (#74)."""

from __future__ import annotations

from pathlib import Path

import pytest

from scripts import check_installed_package as check

SOURCE_ROOT = Path(__file__).resolve().parents[2] / "src" / "osm_polygon_description_tag"


def test_source_data_files_cover_every_packaged_non_python_file() -> None:
    files = check.source_data_files(SOURCE_ROOT)

    assert "_data/ne_110m_land.geojson" in files
    assert "_data/dataset-card-hero.png" in files
    assert "py.typed" in files
    assert not [name for name in files if name.endswith((".py", ".pyc"))]


def test_the_development_install_passes(capsys: pytest.CaptureFixture[str]) -> None:
    assert check.main(["--source-root", str(SOURCE_ROOT)]) == 0
    assert "data files match" in capsys.readouterr().out


def test_missing_and_changed_data_files_are_reported(tmp_path: Path) -> None:
    source = tmp_path / "osm_polygon_description_tag"
    (source / "_data").mkdir(parents=True)
    (source / "_data" / "not-installed.json").write_text("{}")
    (source / "py.typed").write_text("changed")

    problems = list(check.data_file_problems(source))

    assert problems == [
        "missing from the installed package: _data/not-installed.json",
        "differs from the source tree: py.typed",
    ]


def test_a_module_that_fails_to_import_is_reported(monkeypatch: pytest.MonkeyPatch) -> None:
    real = check.importlib.import_module

    def flaky(name: str):
        if name.endswith(".cli"):
            raise ModuleNotFoundError("No module named 'typer'")
        return real(name)

    monkeypatch.setattr(check.importlib, "import_module", flaky)

    problems = list(check.import_problems())

    assert problems == [
        "cannot import osm_polygon_description_tag.cli: "
        "ModuleNotFoundError: No module named 'typer'"
    ]
