"""RED tests proving that arbitrary stdout cannot become the recorded revision.

The orchestrator must consult a Hub verifier after the upload completes; the
upload runner's stdout (or arbitrary return value) is not allowed to become
the recorded remote revision.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from osm_polygon_description_tag.workflow.orchestrator import (
    PUBLICATION_STATE_FILENAME,
    read_publication_state,
    run_and_publish,
)
from tests.helpers.orchestration import (
    fake_exporter as _fake_exporter,
)
from tests.helpers.orchestration import (
    setup_workspace as _setup_workspace,
)

_CLOCK = "2026-07-27T00:00:00+00:00"


def _frozen_clock() -> str:
    return _CLOCK


def test_remote_revision_requires_hub_verifier(tmp_path: Path) -> None:
    """A run without a Hub verifier must fail closed: no state is written."""
    paths, source_root, data_root = _setup_workspace(tmp_path)

    with pytest.raises(Exception, match="verifier|no Hub"):
        run_and_publish(
            paths=paths,
            confirm_repo="NoeFlandre/osm-polygon-description-tag",
            preflight=lambda: {"preflight": "stub", "source_count": 1},
            upload_runner=lambda command: "stdout-revision",
            clock=_frozen_clock,
            exporter=_fake_exporter(),
        )

    state_path = data_root / PUBLICATION_STATE_FILENAME
    assert not state_path.is_file()


def test_recorded_revision_comes_from_verifier_not_stdout(tmp_path: Path) -> None:
    """The recorded revision is exactly what the verifier returned, not the upload stdout."""
    paths, source_root, data_root = _setup_workspace(tmp_path)

    def upload_runner(command: list[str]) -> str:
        # Pretend the upload said something that is NOT a Hub revision.
        return "stdout-says-something-but-must-be-ignored"

    def verifier(repo_id: str, files: tuple[object, ...]) -> str:
        # The real Hub API would query the repo and return its SHA.
        return "verified-hub-sha-deadbeef"

    report = run_and_publish(
        paths=paths,
        confirm_repo="NoeFlandre/osm-polygon-description-tag",
        preflight=lambda: {"preflight": "stub", "source_count": 1},
        upload_runner=upload_runner,
        clock=_frozen_clock,
        exporter=_fake_exporter(),
        verifier=verifier,
    )
    assert report.outcomes[0].remote_revision == "verified-hub-sha-deadbeef"

    state = read_publication_state(data_root)
    assert state["published"]["a.osm.pbf"]["remote_revision"] == "verified-hub-sha-deadbeef"
    # No fabricated "stdout-says-..." should appear anywhere in the state file.
    blob = json.dumps(state)
    assert "stdout-says" not in blob


def test_verifier_failure_means_no_state(tmp_path: Path) -> None:
    """If the verifier raises (or returns empty), no state is recorded."""
    paths, source_root, data_root = _setup_workspace(tmp_path)

    def verifier(repo_id: str, files: tuple[object, ...]) -> str:
        raise RuntimeError("hub query failed")

    with pytest.raises(Exception, match="hub query failed"):
        run_and_publish(
            paths=paths,
            confirm_repo="NoeFlandre/osm-polygon-description-tag",
            preflight=lambda: {"preflight": "stub", "source_count": 1},
            upload_runner=lambda command: "rev",
            clock=_frozen_clock,
            exporter=_fake_exporter(),
            verifier=verifier,
        )
    assert not (data_root / PUBLICATION_STATE_FILENAME).is_file()


def test_verifier_returning_empty_string_means_no_state(tmp_path: Path) -> None:
    """An unverifiable revision must not be recorded as 'unknown'."""
    paths, source_root, data_root = _setup_workspace(tmp_path)

    def verifier(repo_id: str, files: tuple[object, ...]) -> str:
        return ""

    with pytest.raises(Exception, match="verifier|unverifiable|unknown"):
        run_and_publish(
            paths=paths,
            confirm_repo="NoeFlandre/osm-polygon-description-tag",
            preflight=lambda: {"preflight": "stub", "source_count": 1},
            upload_runner=lambda command: "rev",
            clock=_frozen_clock,
            exporter=_fake_exporter(),
            verifier=verifier,
        )
    assert not (data_root / PUBLICATION_STATE_FILENAME).is_file()


def test_final_revision_recorded_only_after_verifier(tmp_path: Path) -> None:
    """The final metadata revision is the verifier result, not the upload stdout."""
    paths, source_root, data_root = _setup_workspace(tmp_path)

    verifier_calls = {"count": 0}

    def verifier(repo_id: str, files: tuple[object, ...]) -> str:
        verifier_calls["count"] += 1
        return f"hub-rev-{verifier_calls['count']}"

    def upload_runner(command: list[str]) -> str:
        return "stdout-ignored"

    report = run_and_publish(
        paths=paths,
        confirm_repo="NoeFlandre/osm-polygon-description-tag",
        preflight=lambda: {"preflight": "stub", "source_count": 1},
        upload_runner=upload_runner,
        clock=_frozen_clock,
        exporter=_fake_exporter(),
        verifier=verifier,
    )
    # At least two verifier calls: per-PBF + final metadata.
    assert verifier_calls["count"] >= 2
    assert report.final_remote_revision == "hub-rev-2"
