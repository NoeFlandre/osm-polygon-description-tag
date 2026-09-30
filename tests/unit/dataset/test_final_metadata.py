"""RED tests for the explicit final-metadata publication path.

The orchestrator must always upload README.md + stats.json once at the end
of a successful run, even when no per-PBF upload occurred in the same
invocation (e.g. after a no-op restart that already published everything).

These tests assert that:

- The final metadata command contains exactly ``README.md`` and ``stats.json``.
- A failed final-metadata upload propagates as ``OrchestratorError`` and
  prevents a successful return.
- A verifier failure on the final metadata is reported and non-zero.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from osm_polygon_description_tag.workflow.orchestrator import (
    OrchestratorError,
    run_and_publish,
)
from tests.helpers.orchestration import (
    fake_exporter as _fake_exporter,
)
from tests.helpers.orchestration import (
    setup_workspace as _setup_workspace,
)


def test_final_metadata_command_contains_only_metadata(tmp_path: Path) -> None:
    """The final metadata command must include only README.md and stats.json."""
    paths, _, data_root = _setup_workspace(tmp_path)
    captured: list[list[str]] = []

    def upload_runner(command: list[str]) -> str:
        captured.append(command)
        return "ok"

    def verifier(repo_id: str, files: tuple[object, ...]) -> str:
        return "verified"

    run_and_publish(
        paths=paths,
        confirm_repo="NoeFlandre/osm-polygon-description-tag",
        preflight=lambda: {"preflight": "stub", "source_count": 1},
        upload_runner=upload_runner,
        clock=lambda: "2026-07-27T00:00:00+00:00",
        exporter=_fake_exporter(),
        verifier=verifier,
    )
    # The final metadata upload is the last upload command.
    final_command = captured[-1]
    includes = [
        final_command[index + 1]
        for index, piece in enumerate(final_command)
        if piece == "--include"
    ]
    assert "README.md" in includes
    assert "stats.json" in includes
    # No fictitious PBF paths.
    for inc in includes:
        assert ".parquet" not in inc
        assert ".manifest.json" not in inc


def test_final_metadata_failure_propagates(tmp_path: Path) -> None:
    """A failed final-metadata upload raises OrchestratorError and exits non-zero."""
    paths, _, data_root = _setup_workspace(tmp_path)

    def upload_runner(command: list[str]) -> str:
        # Fail on the final metadata command (no .parquet include).
        includes = [
            command[index + 1] for index, piece in enumerate(command) if piece == "--include"
        ]
        if all(".parquet" not in inc for inc in includes):
            raise subprocess.CalledProcessError(1, command)
        return "ok"

    def verifier(repo_id: str, files: tuple[object, ...]) -> str:
        return "verified"

    with pytest.raises(OrchestratorError, match="final metadata"):
        run_and_publish(
            paths=paths,
            confirm_repo="NoeFlandre/osm-polygon-description-tag",
            preflight=lambda: {"preflight": "stub", "source_count": 1},
            upload_runner=upload_runner,
            clock=lambda: "2026-07-27T00:00:00+00:00",
            exporter=_fake_exporter(),
            verifier=verifier,
        )


def test_final_metadata_verifier_failure_is_reported(tmp_path: Path) -> None:
    """Verifier failure on the final metadata is reported and non-zero."""
    paths, _, data_root = _setup_workspace(tmp_path)

    def upload_runner(command: list[str]) -> str:
        return "ok"

    def verifier(repo_id: str, files: tuple[object, ...]) -> str:
        # Allow the per-PBF verification; fail on final metadata (5 files:
        # README.md, stats.json, H3 density map, area distribution histogram,
        # dataset card hero).
        if len(files) == 5:
            raise RuntimeError("final hub query failed")
        return "verified"

    with pytest.raises(OrchestratorError, match="final hub query failed"):
        run_and_publish(
            paths=paths,
            confirm_repo="NoeFlandre/osm-polygon-description-tag",
            preflight=lambda: {"preflight": "stub", "source_count": 1},
            upload_runner=upload_runner,
            clock=lambda: "2026-07-27T00:00:00+00:00",
            exporter=_fake_exporter(),
            verifier=verifier,
        )
