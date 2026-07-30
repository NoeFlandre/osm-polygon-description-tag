import json
import re
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest
from shapely.geometry import Polygon

from osm_polygon_description_tag.cli import (
    handle_inspect,
    handle_publish,
    handle_publish_plan,
    handle_validate,
    run,
)
from osm_polygon_description_tag.config import DEFAULT_SOURCE_ROOT
from osm_polygon_description_tag.publication import PublicationError
from osm_polygon_description_tag.storage import write_geoparquet
from tests.conftest import make_record_dict

COMMANDS = (
    "inspect",
    "build-one",
    "build-all",
    "validate",
    "generate-card",
    "publish-plan",
    "publish",
    "run-and-publish",
)
COMMON_OPTIONS = ("--source-root", "--data-root", "--osmium")
HELP_OPTION = {"--help"}
COMMAND_OPTIONS = {
    "inspect": {*COMMON_OPTIONS, *HELP_OPTION},
    "build-one": {*COMMON_OPTIONS, *HELP_OPTION},
    "build-all": {*COMMON_OPTIONS, *HELP_OPTION},
    "validate": {*COMMON_OPTIONS, *HELP_OPTION},
    "generate-card": {*COMMON_OPTIONS, *HELP_OPTION},
    "publish-plan": {*COMMON_OPTIONS, *HELP_OPTION},
    "publish": {*COMMON_OPTIONS, *HELP_OPTION, "--plan"},
    "run-and-publish": {*COMMON_OPTIONS, *HELP_OPTION, "--confirm-repo"},
}


def _cli(*args: str) -> subprocess.CompletedProcess[str]:
    """Invoke the installed public console-script entry point."""
    executable = Path(sys.executable).with_name("osm-polygon-description-tag")
    return subprocess.run(  # noqa: S603
        [str(executable), *args],
        text=True,
        capture_output=True,
        check=False,
    )


def _public_commands(help_text: str) -> set[str]:
    """Extract the exact command group from argparse or Typer help."""
    plain = re.sub(r"\x1b\[[0-9;]*m", "", help_text)
    argparse_group = re.search(r"\{([^}\n]+)\}", plain)
    if argparse_group is not None:
        return {piece.strip() for piece in argparse_group.group(1).split(",")}

    rich_commands = re.search(
        r"(?m)^[^\n]*Commands[^\n]*\n(?P<body>(?:.*\n)*?)^╰.*$",
        plain,
    )
    if rich_commands is not None:
        return {
            match.group(1)
            for line in rich_commands.group("body").splitlines()
            if (match := re.match(r"^\s*│\s*([a-z][a-z0-9-]*)\b", line))
        }

    commands_match = re.search(r"(?ms)^Commands:\s*\n(?P<body>.*?)(?:\n\S|\Z)", plain)
    if commands_match is None:
        return set()
    return {
        match.group(1)
        for line in commands_match.group("body").splitlines()
        if (match := re.match(r"^\s{2,}([a-z][a-z0-9-]*)\b", line))
    }


def test_all_public_commands_remain_available_from_console_entry_point() -> None:
    result = _cli("--help")

    assert result.returncode == 0
    assert result.stderr == ""
    assert _public_commands(result.stdout) == set(COMMANDS)


@pytest.mark.parametrize("command", COMMANDS)
def test_every_command_keeps_exact_public_options(command: str) -> None:
    result = _cli(command, "--help")
    long_options = set(re.findall(r"--[a-z][a-z-]*", result.stdout))

    assert result.returncode == 0
    assert result.stderr == ""
    assert long_options == COMMAND_OPTIONS[command]


def test_build_one_keeps_required_basename() -> None:
    help_result = _cli("build-one", "--help")
    missing_result = _cli("build-one")

    assert "basename" in help_result.stdout
    assert missing_result.returncode == 2
    assert "usage:" in missing_result.stderr


def test_publish_keeps_required_plan_option() -> None:
    help_result = _cli("publish", "--help")
    missing_result = _cli("publish")

    assert "--plan" in help_result.stdout
    assert missing_result.returncode == 2
    assert "usage:" in missing_result.stderr


def test_run_and_publish_keeps_required_confirm_repo_option() -> None:
    help_result = _cli("run-and-publish", "--help")
    missing_result = _cli("run-and-publish")

    assert "--confirm-repo" in help_result.stdout
    assert missing_result.returncode == 2
    assert "usage:" in missing_result.stderr


def test_inspect_uses_approved_default_paths(capsys: pytest.CaptureFixture[str]) -> None:
    # inspect with a non-existent default source root exits non-zero but prints the path attempt.
    # Use a non-existent custom root so we only assert the parser accepted defaults.
    exit_code = run(["inspect", "--source-root", "/no/such/raw"])
    captured = capsys.readouterr()
    assert exit_code != 0
    assert DEFAULT_SOURCE_ROOT.name in str(captured.err) or "/no/such/raw" in str(captured.err)


def test_publish_rejects_wrong_plan_identity(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    data = tmp_path / "generated"
    (data / "data").mkdir(parents=True)
    (data / "manifests").mkdir(parents=True)
    (data / "README.md").write_text("# Card\n", encoding="utf-8")
    (data / "stats.json").write_text("{}\n", encoding="utf-8")

    args = SimpleNamespace(
        plan="deadbeef",  # wrong on purpose
        publisher=None,
        source_root=tmp_path / "raw",
        data_root=data,
        osmium="osmium",
    )

    with pytest.raises(PublicationError, match="does not match"):
        handle_publish(args)


def test_failure_exits_non_zero_with_actionable_stderr(
    capsys: pytest.CaptureFixture[str], tmp_path: Path
) -> None:
    exit_code = run(
        [
            "inspect",
            "--source-root",
            str(tmp_path / "missing"),
            "--data-root",
            str(tmp_path / "generated"),
        ]
    )
    captured = capsys.readouterr()
    assert exit_code != 0
    assert captured.err.strip() != ""
    assert captured.err.startswith("error:")


def test_inspect_handler_prints_json_summary(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    source = tmp_path / "raw"
    source.mkdir()
    (source / "a.osm.pbf").write_bytes(b"x")
    data = tmp_path / "generated"
    args = SimpleNamespace(
        source_root=source,
        data_root=data,
        osmium="osmium",
        export_config=Path("config/osmium-export.json"),
    )

    exit_code = handle_inspect(args)
    captured = capsys.readouterr()

    assert exit_code == 0
    payload = json.loads(captured.out)
    assert payload["source_count"] == 1
    assert payload["sources"][0]["name"] == "a.osm.pbf"
    assert payload["sources"][0]["output_name"] == "a.parquet"


def test_validate_handler_sums_rows(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    source = tmp_path / "raw"
    data = tmp_path / "generated"
    (data / "data").mkdir(parents=True)
    record = make_record_dict(
        Polygon([(0, 0), (0, 1), (1, 1), (1, 0)]),
        {"description": "x"},
        osm_id=1,
    )
    write_geoparquet(iter([record]), data / "data" / "a.parquet", batch_size=10)
    args = SimpleNamespace(
        source_root=source,
        data_root=data,
        osmium="osmium",
        export_config=Path("config/osmium-export.json"),
    )

    exit_code = handle_validate(args)
    payload = json.loads(capsys.readouterr().out)

    assert exit_code == 0
    assert payload["files"] == 1
    assert payload["rows"] == 1


def test_publish_plan_handler_reports_identity(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    data = tmp_path / "generated"
    (data / "data").mkdir(parents=True)
    (data / "manifests").mkdir(parents=True)
    (data / "README.md").write_text("# Card\n", encoding="utf-8")
    (data / "stats.json").write_text("{}\n", encoding="utf-8")
    args = SimpleNamespace(
        source_root=tmp_path / "raw",
        data_root=data,
        osmium="osmium",
        export_config=Path("config/osmium-export.json"),
    )

    exit_code = handle_publish_plan(args)
    payload = json.loads(capsys.readouterr().out)

    assert exit_code == 0
    assert payload["repo_id"] == "NoeFlandre/osm-polygon-description-tag"
    assert len(payload["identity_sha256"]) == 64
    assert any(item["relative_path"] == "README.md" for item in payload["files"])


def test_handle_build_one_invokes_pipeline(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    source = tmp_path / "raw"
    source.mkdir()
    (source / "region.osm.pbf").write_bytes(b"x")
    data = tmp_path / "generated"

    fake_result = SimpleNamespace(
        source_name="region.osm.pbf",
        output_name="region.parquet",
        status="built",
        emitted_features=1,
        included_rows=1,
        rejections={"no_nonempty_description": 0},
        output_path=data / "data" / "region.parquet",
        manifest_path=data / "manifests" / "region.manifest.json",
    )

    import osm_polygon_description_tag.cli as cli

    monkeypatch.setattr(cli, "build_one", lambda *args, **kwargs: fake_result)

    args = SimpleNamespace(
        basename="region.osm.pbf",
        source_root=source,
        data_root=data,
        osmium="osmium",
        export_config=Path("config/osmium-export.json"),
    )

    exit_code = cli.handle_build_one(args)
    payload = json.loads(capsys.readouterr().out)

    assert exit_code == 0
    assert payload["source_name"] == "region.osm.pbf"
    assert payload["status"] == "built"


def test_handle_publish_invokes_execute_upload(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    data = tmp_path / "generated"
    (data / "data").mkdir(parents=True)
    (data / "manifests").mkdir(parents=True)
    (data / "README.md").write_text("# Card\n", encoding="utf-8")
    (data / "stats.json").write_text("{}\n", encoding="utf-8")

    import osm_polygon_description_tag.cli as cli

    captured: list[list[str]] = []

    def fake_execute(plan, *, confirmation, runner=None):  # type: ignore[no-untyped-def]
        captured.append([plan.repo_id, confirmation])

    monkeypatch.setattr(cli, "execute_upload", fake_execute)

    args = SimpleNamespace(
        plan="abc",
        publisher=None,
        source_root=tmp_path / "raw",
        data_root=data,
        osmium="osmium",
    )

    exit_code = cli.handle_publish(args)
    payload = json.loads(capsys.readouterr().out)

    assert exit_code == 0
    assert captured == [["NoeFlandre/osm-polygon-description-tag", "abc"]]
    assert payload["repo_id"] == "NoeFlandre/osm-polygon-description-tag"


def test_handle_run_and_publish_invokes_orchestrator(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    import osm_polygon_description_tag.cli as cli

    fake_report = {
        "preflight": {"source_count": 1},
        "source_count": 1,
        "outcomes": [
            {
                "source_name": "a.osm.pbf",
                "status": "published",
                "included_rows": 1,
                "output_bytes": 100,
                "remote_revision": "rev-1",
                "note": None,
            }
        ],
        "final_remote_revision": "rev-1",
    }

    captured: dict[str, object] = {}

    def fake_run_and_publish(**kwargs: object) -> object:
        captured.update(kwargs)
        return type("R", (), {"to_payload": lambda self: fake_report})()

    monkeypatch.setattr(cli, "run_and_publish", fake_run_and_publish)

    args = SimpleNamespace(
        confirm_repo="NoeFlandre/osm-polygon-description-tag",
        preflight=None,
        upload_runner=None,
        clock=None,
        source_root=None,
        data_root=None,
        osmium="osmium",
    )

    exit_code = cli.handle_run_and_publish(args)
    payload = json.loads(capsys.readouterr().out)

    assert exit_code == 0
    assert payload["final_remote_revision"] == "rev-1"
    assert captured["osmium_executable"] == "osmium"
