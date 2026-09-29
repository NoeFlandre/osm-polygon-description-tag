"""Acceptance: the pipeline refuses unsafe or unusable setups with one actionable line.

User story: as an operator I would rather be told, before anything is built or
uploaded, exactly what to fix. Each scenario runs the public CLI and checks the
exit code and the single ``error:`` line.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from osm_polygon_description_tag.cli import run
from osm_polygon_description_tag.publication import REPO_ID

EXIT_ENVIRONMENT = 3


@pytest.fixture(autouse=True)
def _no_ambient_roots(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("OSM_POLYGON_SOURCE_ROOT", raising=False)
    monkeypatch.delenv("OSM_POLYGON_DATA_ROOT", raising=False)


def _roots(tmp_path: Path) -> list[str]:
    source, data = tmp_path / "raw", tmp_path / "data"
    source.mkdir()
    data.mkdir()
    (source / "a.osm.pbf").write_bytes(b"pbf")
    return ["--source-root", str(source), "--data-root", str(data)]


def _error_lines(capsys: pytest.CaptureFixture[str]) -> list[str]:
    return [line for line in capsys.readouterr().err.splitlines() if line.startswith("error: ")]


def test_given_no_osmium_when_run_then_it_says_which_executable_is_missing(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    code = run(
        [
            *("run-and-publish", "--confirm-repo", REPO_ID, *_roots(tmp_path)),
            *("--osmium", "definitely-not-installed"),
        ]
    )

    assert code == EXIT_ENVIRONMENT
    assert _error_lines(capsys) == ["error: osmium executable not found: definitely-not-installed"]


def test_given_a_binary_that_is_not_osmium_when_run_then_it_is_refused(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    dummy = tmp_path / "dummy-osmium"
    dummy.write_text("#!/bin/sh\necho not osmium\n", encoding="utf-8")
    dummy.chmod(0o755)

    code = run(
        ["run-and-publish", "--confirm-repo", REPO_ID, *_roots(tmp_path), "--osmium", str(dummy)]
    )

    errors = _error_lines(capsys)
    assert code == EXIT_ENVIRONMENT
    assert len(errors) == 1
    assert "does not look like a real osmium-tool binary" in errors[0]


def test_given_the_wrong_repo_when_run_then_nothing_starts(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    code = run(["run-and-publish", "--confirm-repo", "someone/else", *_roots(tmp_path)])

    assert code == EXIT_ENVIRONMENT
    assert _error_lines(capsys) == [
        f"error: --confirm-repo must equal '{REPO_ID}' (got 'someone/else')"
    ]
    assert list((tmp_path / "data").iterdir()) == []


def test_given_no_roots_when_run_then_it_names_what_to_set(
    capsys: pytest.CaptureFixture[str],
) -> None:
    code = run(["run-and-publish", "--confirm-repo", REPO_ID])

    errors = _error_lines(capsys)
    assert code == EXIT_ENVIRONMENT
    assert len(errors) == 1
    assert "OSM_POLYGON_SOURCE_ROOT" in errors[0]
