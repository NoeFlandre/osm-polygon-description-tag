"""Startup-cost contracts for lightweight dataset submodules."""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path


def test_language_checkpoint_import_does_not_start_plotting_stack(tmp_path: Path) -> None:
    """Language workers must not import Matplotlib just to read checkpoints."""
    env = os.environ.copy()
    env["MPLCONFIGDIR"] = str(tmp_path / "matplotlib")
    result = subprocess.run(  # noqa: S603 - executable and arguments are test-owned
        [
            sys.executable,
            "-c",
            (
                "import sys; "
                "from osm_polygon_description_tag.dataset.languages import checkpoint; "
                "assert 'matplotlib' not in sys.modules; "
                "assert checkpoint.CHECKPOINT_SCHEMA_VERSION == 1"
            ),
        ],
        check=True,
        env=env,
        capture_output=True,
        text=True,
        timeout=10,
    )
    assert result.stderr == ""
