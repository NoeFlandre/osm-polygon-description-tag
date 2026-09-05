"""RED tests proving the public CLI has no test-hook injection flags.

The public ``run-and-publish`` command must accept only ``--confirm-repo``
(and the standard shared ``--source-root`` / ``--data-root`` / ``--osmium``
flags). The internal ``--preflight``, ``--upload-runner``, ``--publisher``,
and ``--clock`` hooks must not appear in the public CLI surface.
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path


def _subparser_text(command: str) -> str:
    """Return public console help without depending on a parser implementation."""
    executable = Path(sys.executable).with_name("osm-polygon-description-tag")
    result = subprocess.run(  # noqa: S603
        [str(executable), command, "--help"],
        text=True,
        capture_output=True,
        check=False,
    )
    assert result.returncode == 0
    assert result.stderr == ""
    return result.stdout


def test_run_and_publish_does_not_expose_preflight_flag() -> None:
    """``--preflight`` is not a public CLI flag."""
    text = _subparser_text("run-and-publish")
    assert "--preflight" not in text


def test_run_and_publish_does_not_expose_upload_runner_flag() -> None:
    """``--upload-runner`` is not a public CLI flag."""
    text = _subparser_text("run-and-publish")
    assert "--upload-runner" not in text


def test_run_and_publish_does_not_expose_clock_flag() -> None:
    """``--clock`` is not a public CLI flag."""
    text = _subparser_text("run-and-publish")
    assert "--clock" not in text


def test_publish_does_not_expose_publisher_flag() -> None:
    """The ``publish`` subcommand must not expose ``--publisher``."""
    text = _subparser_text("publish")
    assert "--publisher" not in text
