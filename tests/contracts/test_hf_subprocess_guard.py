"""The test suite must fail closed before a real Hugging Face CLI launch."""

from __future__ import annotations

import subprocess

import pytest


def test_unmocked_hf_command_is_rejected_before_process_launch() -> None:
    with pytest.raises(RuntimeError, match="real Hugging Face CLI"):
        subprocess.run(["hf", "auth", "whoami"], check=True)  # noqa: S607
