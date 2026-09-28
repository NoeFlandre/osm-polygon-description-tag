"""Preflight subprocess failures must fail closed as PreflightError (#70)."""

from __future__ import annotations

import stat
from pathlib import Path

import pytest

from osm_polygon_description_tag import cli
from osm_polygon_description_tag.workflow import preflight
from osm_polygon_description_tag.workflow.preflight import PreflightError


def _script(tmp_path: Path, body: str, *, executable: bool = True) -> Path:
    path = tmp_path / "fake-tool"
    path.write_text(f"#!/bin/sh\n{body}\n")
    mode = stat.S_IRUSR | stat.S_IWUSR | (stat.S_IXUSR if executable else 0)
    path.chmod(mode)
    return path


@pytest.fixture
def short_timeout(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(preflight, "PROBE_TIMEOUT_SECONDS", 0.2)


@pytest.mark.usefixtures("short_timeout")
def test_hanging_osmium_raises_preflight_error(tmp_path: Path) -> None:
    binary = str(_script(tmp_path, "sleep 5"))

    with pytest.raises(PreflightError, match=r"osmium --version failed .*timed out"):
        preflight._run_osmium_version(binary, "osmium")


def test_non_executable_osmium_raises_preflight_error(tmp_path: Path) -> None:
    binary = str(_script(tmp_path, "echo osmium version 1", executable=False))

    with pytest.raises(PreflightError, match="osmium --version failed .*Permission denied"):
        preflight._run_osmium_version(binary, "osmium")


@pytest.mark.usefixtures("short_timeout")
def test_hanging_hf_auth_check_raises_preflight_error(tmp_path: Path) -> None:
    with pytest.raises(PreflightError, match="hf authentication check failed"):
        preflight._hf_cli_identity(str(_script(tmp_path, "sleep 5")))


def test_non_executable_hf_raises_preflight_error(tmp_path: Path) -> None:
    binary = str(_script(tmp_path, "echo user", executable=False))

    with pytest.raises(PreflightError, match="hf authentication check failed"):
        preflight._hf_cli_identity(binary)


@pytest.mark.usefixtures("short_timeout")
def test_cli_reports_preflight_timeout_without_traceback(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    binary = str(_script(tmp_path, "sleep 5"))

    def app(*_args: object, **_kwargs: object) -> int:
        preflight._run_osmium_version(binary, "osmium")
        return 0

    monkeypatch.setattr(cli, "_invoke_app", app)

    assert cli.run([]) == 3  # environment failure
    stderr = capsys.readouterr().err
    assert "osmium --version failed" in stderr
    assert "Traceback" not in stderr
