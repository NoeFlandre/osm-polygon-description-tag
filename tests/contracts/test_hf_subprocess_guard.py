"""The test suite must fail closed before a real Hugging Face CLI launch."""

from __future__ import annotations

import subprocess

import pytest

from tests.conftest import _reject_live_hf_command


def test_unmocked_hf_command_is_rejected_before_process_launch() -> None:
    with pytest.raises(RuntimeError, match="real Hugging Face CLI"):
        subprocess.run(["hf", "auth", "whoami"], check=True)  # noqa: S607


def test_shell_hf_command_is_rejected_before_process_launch() -> None:
    with pytest.raises(RuntimeError, match="real Hugging Face CLI"):
        subprocess.run(  # noqa: S602
            "hf upload-large-folder owner/repo data",  # noqa: S607
            shell=True,
            check=True,
        )


@pytest.mark.parametrize(
    "command",
    [
        "hf upload-large-folder owner/repo data",
        b"hf upload owner/repo file",
        ["/opt/tools/hf", "auth", "whoami"],
    ],
)
def test_hf_command_forms_are_rejected_without_launch(command: object) -> None:
    with pytest.raises(RuntimeError, match="real Hugging Face CLI"):
        _reject_live_hf_command(command)


@pytest.mark.parametrize(
    "command",
    [
        "echo harmless",
        ["/opt/homebrew/bin/osmium", "cat", "synthetic.osm", "-o", "synthetic.osm.pbf"],
    ],
)
def test_unrelated_shell_and_osmium_commands_remain_permitted(command: object) -> None:
    _reject_live_hf_command(command)
