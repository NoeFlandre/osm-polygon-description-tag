"""Bringing the site's record of a submission back into the owned run.

A bounded Grid'5000 job is submitted from the site's frontend, against the
site's copy of the run directory, so the durable submission intent is written
*there*. Collection retrieves that copy. Without adopting its intent, the owned
run directory holds a bundle that nothing claims to have submitted, and the
acknowledgment that closes the shard cannot be written at all.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from osm_polygon_description_tag.workflow.grid_operator import (
    GridOperatorError,
    JobBundle,
    SubmissionIntent,
    adopt_retrieved_intent,
    job_paths,
)
from tests.helpers.messages import exactly

SHARD = "region.parquet"


def _bundle(shard: str = SHARD) -> JobBundle:
    return JobBundle("a" * 64, "b" * 64, "c" * 64, "d" * 64, shard, "e" * 64, 1024, 184)


BUNDLE_ID = _bundle().bundle_id


def _intent(**overrides: object) -> SubmissionIntent:
    fields: dict[str, object] = {
        "bundle_id": BUNDLE_ID,
        "shard": SHARD,
        "job_name": "lang-bbbbbbbbbbbbbbbb",
        "walltime_seconds": 1800,
        "cores": 1,
        "recorded_at": "2026-09-12T21:31:50+00:00",
        "job_id": 6923197,
        "outcome": "submitted",
        "terminal_state": "terminated",
    }
    fields.update(overrides)
    return SubmissionIntent(**fields)  # type: ignore[arg-type]


def _retrieved(tmp_path: Path, intent: SubmissionIntent) -> Path:
    retrieved = tmp_path / "retrieved"
    paths = job_paths(retrieved, _bundle())
    paths.root.mkdir(parents=True)
    paths.intent.write_text(json.dumps(intent.to_payload()), encoding="utf-8")
    return retrieved


def test_the_sites_intent_is_adopted_when_the_owned_run_has_none(tmp_path: Path) -> None:
    """Without this the shard can never be acknowledged, only re-run."""
    run_dir = tmp_path / "run"
    owned = job_paths(run_dir, _bundle())
    owned.root.mkdir(parents=True)
    retrieved = _retrieved(tmp_path, _intent())

    adopted = adopt_retrieved_intent(owned, retrieved, _bundle())

    assert adopted == owned.intent
    assert json.loads(owned.intent.read_text(encoding="utf-8"))["job_id"] == 6923197


def test_an_intent_already_recorded_here_is_never_overwritten(tmp_path: Path) -> None:
    """This run's own record wins; a stale copy must not undo an acknowledgment."""
    run_dir = tmp_path / "run"
    owned = job_paths(run_dir, _bundle())
    owned.root.mkdir(parents=True)
    mine = _intent(result_acknowledged=True, collected_at="2026-09-12T21:40:00+00:00")
    owned.intent.write_text(json.dumps(mine.to_payload()), encoding="utf-8")
    retrieved = _retrieved(tmp_path, _intent())

    assert adopt_retrieved_intent(owned, retrieved, _bundle()) is None
    assert json.loads(owned.intent.read_text(encoding="utf-8"))["result_acknowledged"] is True


def test_an_intent_bound_to_another_shard_is_refused(tmp_path: Path) -> None:
    """Adopting the wrong shard's record would acknowledge work never done."""
    run_dir = tmp_path / "run"
    owned = job_paths(run_dir, _bundle())
    owned.root.mkdir(parents=True)
    retrieved = _retrieved(tmp_path, _intent(shard="elsewhere.parquet"))

    with pytest.raises(
        GridOperatorError,
        match=exactly(
            "retrieved submission intent is not bound to this bundle: "
            f"{job_paths(retrieved, _bundle()).intent}"
        ),
    ):
        adopt_retrieved_intent(owned, retrieved, _bundle())


def test_an_intent_bound_to_another_bundle_is_refused(tmp_path: Path) -> None:
    run_dir = tmp_path / "run"
    owned = job_paths(run_dir, _bundle())
    owned.root.mkdir(parents=True)
    retrieved = _retrieved(tmp_path, _intent(bundle_id="c" * 64))

    with pytest.raises(GridOperatorError, match="not bound to this bundle"):
        adopt_retrieved_intent(owned, retrieved, _bundle())


def test_a_retrieval_without_any_intent_is_refused_by_its_path(tmp_path: Path) -> None:
    """Silence here would look like a job that was never submitted."""
    run_dir = tmp_path / "run"
    owned = job_paths(run_dir, _bundle())
    owned.root.mkdir(parents=True)
    retrieved = tmp_path / "retrieved"
    retrieved.mkdir()

    with pytest.raises(
        GridOperatorError,
        match=exactly(
            f"retrieved run has no submission intent: {job_paths(retrieved, _bundle()).intent}"
        ),
    ):
        adopt_retrieved_intent(owned, retrieved, _bundle())


def test_a_retrieved_intent_that_is_a_symlink_is_refused(tmp_path: Path) -> None:
    run_dir = tmp_path / "run"
    owned = job_paths(run_dir, _bundle())
    owned.root.mkdir(parents=True)
    retrieved = tmp_path / "retrieved"
    incoming = job_paths(retrieved, _bundle())
    incoming.root.mkdir(parents=True)
    real = tmp_path / "elsewhere.json"
    real.write_text(json.dumps(_intent().to_payload()), encoding="utf-8")
    incoming.intent.symlink_to(real)

    with pytest.raises(GridOperatorError, match="must be a regular file"):
        adopt_retrieved_intent(owned, retrieved, _bundle())


def test_the_intent_is_adopted_into_a_run_that_has_no_jobs_directory_yet(
    tmp_path: Path,
) -> None:
    """Adoption must create the whole owned job path, not just its last segment.

    A run directory that never staged this shard locally has no ``jobs``
    directory at all, so the parent has to be created too. Creating only the
    leaf raises and the shard can never be acknowledged.
    """
    run_dir = tmp_path / "run"
    run_dir.mkdir()
    owned = job_paths(run_dir, _bundle())
    assert not owned.root.parent.exists()
    retrieved = _retrieved(tmp_path, _intent())

    adopted = adopt_retrieved_intent(owned, retrieved, _bundle())

    assert adopted == owned.intent
    assert json.loads(owned.intent.read_text(encoding="utf-8"))["job_id"] == 6923197


def test_an_owned_intent_that_is_a_symlink_is_refused_by_its_path(tmp_path: Path) -> None:
    """Following a symlink here would write this run's record somewhere else."""
    run_dir = tmp_path / "run"
    owned = job_paths(run_dir, _bundle())
    owned.root.mkdir(parents=True)
    elsewhere = tmp_path / "elsewhere.json"
    elsewhere.write_text(json.dumps(_intent().to_payload()), encoding="utf-8")
    owned.intent.symlink_to(elsewhere)
    retrieved = _retrieved(tmp_path, _intent())

    with pytest.raises(
        GridOperatorError,
        match=exactly(f"submission intent must be a regular file: {owned.intent}"),
    ):
        adopt_retrieved_intent(owned, retrieved, _bundle())
