"""Tests for rejecting dummy osmium binaries."""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from tests.helpers.osmium import real_osmium_path as _real_osmium_path


def test_the_real_osmium_helper_skips_on_a_dummy_binary(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A dummy ``osmium`` that returns no libosmium version is rejected."""
    dummy = tmp_path / "dummy-osmium"
    dummy.write_text("#!/bin/sh\necho nope\n", encoding="utf-8")
    dummy.chmod(0o755)

    monkeypatch.setattr("shutil.which", lambda name: str(dummy) if name == "osmium" else None)

    with pytest.raises(pytest.skip.Exception):
        _real_osmium_path()


def test_real_osmium_detects_dummy_executable(tmp_path: Path) -> None:
    """A symlink to ``/usr/bin/true`` is rejected by the version probe."""
    if not Path("/usr/bin/true").exists():
        pytest.skip("/usr/bin/true not available on this platform")
    dummy = tmp_path / "true-osmium"
    try:
        dummy.symlink_to("/usr/bin/true")
    except OSError:
        pytest.skip("cannot create symlink")
    completed = subprocess.run(  # noqa: S603 - controlled argument array, no shell
        [str(dummy), "--version"], check=True, capture_output=True, text=True, timeout=5
    )
    output = completed.stdout or completed.stderr or ""
    assert "libosmium" not in output
    assert "osmium version" not in output
