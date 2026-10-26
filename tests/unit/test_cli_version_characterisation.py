"""The global ``--version`` option prints the installed version and exits cleanly."""

from __future__ import annotations

from importlib.metadata import version

import pytest

from osm_polygon_description_tag.cli import run


def test_version_flag_prints_the_distribution_version_and_exits_zero(
    capsys: pytest.CaptureFixture[str],
) -> None:
    assert run(["--version"]) == 0

    assert capsys.readouterr().out == f"{version('osm-polygon-description-tag')}\n"


def test_help_advertises_the_version_option(capsys: pytest.CaptureFixture[str]) -> None:
    assert run(["--help"]) == 0

    assert "--version" in capsys.readouterr().out
