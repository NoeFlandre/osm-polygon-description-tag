"""Helpers for tests that drive a real ``osmium`` binary."""

from __future__ import annotations

import subprocess
from pathlib import Path


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
