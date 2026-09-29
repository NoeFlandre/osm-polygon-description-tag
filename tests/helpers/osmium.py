"""Helpers for tests that drive a real ``osmium`` binary."""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest


def real_osmium_path() -> str:
    """Return a verified real ``osmium`` binary or skip when it is unavailable."""
    path = shutil.which("osmium")
    if path is None:
        pytest.skip("osmium executable is required for the synthetic end-to-end test")
    completed = subprocess.run(  # noqa: S603 - controlled argument array, no shell
        [path, "--version"], check=True, capture_output=True, text=True, timeout=15
    )
    output = completed.stdout or completed.stderr or ""
    if "libosmium" not in output and "osmium version" not in output:
        pytest.skip(f"osmium at {path!r} does not look like a real osmium-tool binary: {output!r}")
    if not output.strip():
        pytest.skip(f"osmium at {path!r} produced no output")
    return path


def write_pbf(executable: str, osm_path: Path, pbf_path: Path) -> None:
    """Convert an ``.osm`` XML fixture to ``.osm.pbf`` with ``osmium cat``."""
    completed = subprocess.run(  # noqa: S603 - controlled argument array, no shell
        [executable, "cat", str(osm_path), "-o", str(pbf_path), "--overwrite"],
        check=True,
        capture_output=True,
        shell=False,
        timeout=30,
    )
    assert completed.returncode == 0, completed.stderr.decode("utf-8", errors="replace")
