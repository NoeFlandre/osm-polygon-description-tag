"""The typed requests the migrated Typer commands hand to their handlers."""

from __future__ import annotations

from pathlib import Path

import pytest
from typer.testing import CliRunner

from osm_polygon_description_tag import cli
from osm_polygon_description_tag.cli_requests import BuildOneRequest, PathOptions

_RUNNER = CliRunner()


def test_build_one_request_extends_path_options_with_the_basename(tmp_path: Path) -> None:
    request = BuildOneRequest(source_root=None, data_root=tmp_path, osmium="osmium", basename="a")

    assert isinstance(request, PathOptions)
    assert request.basename == "a"


@pytest.mark.parametrize(
    ("command", "handler"),
    [
        ("inspect", "handle_inspect"),
        ("build-all", "handle_build_all"),
        ("validate", "handle_validate"),
        ("generate-card", "handle_card"),
        ("migrate-schema", "handle_migrate_schema"),
    ],
)
def test_path_commands_hand_their_options_to_the_handler_as_path_options(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, command: str, handler: str
) -> None:
    seen: list[object] = []
    monkeypatch.setattr(cli, handler, lambda args: seen.append(args) or 0)
    source, data = tmp_path / "raw", tmp_path / "out"

    result = _RUNNER.invoke(
        cli.app,
        [command, "--source-root", str(source), "--data-root", str(data), "--osmium", "x"],
    )

    assert result.exit_code == 0
    assert seen == [PathOptions(source_root=source, data_root=data, osmium="x")]


def test_build_one_hands_its_basename_to_the_handler_as_a_request(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    seen: list[object] = []
    monkeypatch.setattr(cli, "handle_build_one", lambda args: seen.append(args) or 0)

    result = _RUNNER.invoke(
        cli.app, ["build-one", "region.osm.pbf", "--source-root", str(tmp_path / "raw")]
    )

    assert result.exit_code == 0
    assert seen == [
        BuildOneRequest(
            source_root=tmp_path / "raw", data_root=None, osmium="osmium", basename="region.osm.pbf"
        )
    ]
