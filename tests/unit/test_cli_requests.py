"""The typed requests the migrated Typer commands hand to their handlers."""

from __future__ import annotations

import dataclasses
from pathlib import Path

import pytest
from typer.testing import CliRunner

from osm_polygon_description_tag import cli
from osm_polygon_description_tag.cli_requests import (
    BuildOneRequest,
    MigrateTextRequest,
    PathOptions,
    PublishRequest,
    ReleaseStatsRequest,
    RunAndPublishRequest,
    TrackioSnapshotRequest,
)
from osm_polygon_description_tag.runtime.presentation import TerminalPresenter

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


@pytest.mark.parametrize(
    ("command", "handler", "extra_args", "expected"),
    [
        (
            "migrate-text",
            "handle_migrate_text",
            ["--max-workers", "4"],
            lambda source, data: MigrateTextRequest(
                source_root=source, data_root=data, osmium="x", max_workers=4
            ),
        ),
        (
            "trackio-snapshot",
            "handle_trackio_snapshot",
            ["--project", "p", "--space-id", "owner/space", "--run-name", "run"],
            lambda source, data: TrackioSnapshotRequest(
                source_root=source,
                data_root=data,
                osmium="x",
                project="p",
                space_id="owner/space",
                run_name="run",
            ),
        ),
        (
            "publish-plan",
            "handle_publish_plan",
            [],
            lambda source, data: PathOptions(source_root=source, data_root=data, osmium="x"),
        ),
        (
            "publish",
            "handle_publish",
            ["--plan", "abc"],
            lambda source, data: PublishRequest(
                source_root=source, data_root=data, osmium="x", plan="abc"
            ),
        ),
        (
            "release-stats",
            "handle_release_stats",
            ["--confirm-repo", "owner/dataset", "--apply"],
            lambda source, data: ReleaseStatsRequest(
                source_root=source,
                data_root=data,
                osmium="x",
                confirm_repo="owner/dataset",
                apply=True,
            ),
        ),
    ],
)
def test_remaining_commands_hand_their_typed_request_to_the_handler(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    command: str,
    handler: str,
    extra_args: list[str],
    expected: object,
) -> None:
    seen: list[object] = []
    monkeypatch.setattr(cli, handler, lambda args: seen.append(args) or 0)
    source, data = tmp_path / "raw", tmp_path / "out"

    result = _RUNNER.invoke(
        cli.app,
        [
            command,
            *extra_args,
            "--source-root",
            str(source),
            "--data-root",
            str(data),
            "--osmium",
            "x",
        ],
    )

    assert result.exit_code == 0
    assert seen == [expected(source, data)]  # type-exact: dataclass equality checks the class


def test_run_and_publish_hands_its_request_and_verbosity_to_the_handler(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    seen: list[RunAndPublishRequest] = []
    monkeypatch.setattr(cli, "handle_run_and_publish", lambda args: seen.append(args) or 0)

    result = _RUNNER.invoke(
        cli.app,
        [
            "-q",
            "run-and-publish",
            "--confirm-repo",
            "owner/dataset",
            "--source-root",
            str(tmp_path / "raw"),
            "--data-root",
            str(tmp_path / "out"),
        ],
    )

    assert result.exit_code == 0
    assert len(seen) == 1
    request = seen[0]
    assert type(request) is RunAndPublishRequest
    assert request.confirm_repo == "owner/dataset"
    assert request.stderr_level == "WARNING"
    assert isinstance(request.presenter, TerminalPresenter)


def test_run_and_publish_request_defaults_match_a_plain_invocation(tmp_path: Path) -> None:
    request = RunAndPublishRequest(
        source_root=None, data_root=tmp_path, osmium="osmium", confirm_repo="owner/dataset"
    )

    assert request.presenter is None
    assert request.stderr_level == "INFO"


def test_typed_requests_are_immutable(tmp_path: Path) -> None:
    request = MigrateTextRequest(source_root=None, data_root=tmp_path, osmium="x", max_workers=1)

    with pytest.raises(dataclasses.FrozenInstanceError):
        request.max_workers = 2  # type: ignore[misc]
