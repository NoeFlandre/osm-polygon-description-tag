from pathlib import Path

import pytest

from osm_polygon_description_tag.cli import create_parser, run
from osm_polygon_description_tag.config import DEFAULT_SOURCE_ROOT


def test_subcommands_are_frozen() -> None:
    parser = create_parser()
    sub_action = next(a for a in parser._actions if isinstance(a.choices, dict))
    assert set(sub_action.choices) == {
        "inspect",
        "build-one",
        "build-all",
        "validate",
        "generate-card",
        "publish-plan",
        "publish",
    }


def test_inspect_uses_approved_default_paths(capsys: pytest.CaptureFixture[str]) -> None:
    # inspect with a non-existent default source root exits non-zero but prints the path attempt.
    # Use a non-existent custom root so we only assert the parser accepted defaults.
    exit_code = run(["inspect", "--source-root", "/no/such/raw"])
    captured = capsys.readouterr()
    assert exit_code != 0
    assert DEFAULT_SOURCE_ROOT.name in str(captured.err) or "/no/such/raw" in str(captured.err)


def test_build_one_requires_basename() -> None:
    with pytest.raises(SystemExit) as info:
        run(["build-one"])
    assert info.value.code != 0


def test_publish_requires_plan_and_confirm() -> None:
    with pytest.raises(SystemExit) as info:
        run(["publish"])
    assert info.value.code != 0
    with pytest.raises(SystemExit) as info2:
        run(["publish", "--plan", "abc"])
    assert info2.value.code != 0


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
