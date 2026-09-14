"""Choosing the scheduler queue explicitly, per site.

Grid'5000 sites do not agree on what a bare ``oarsub`` means. Four of the eight
sites used here auto-select a queue named ``abaca`` that does not exist and
reject the submission outright; the rest accept the bare form. The queue is
therefore a per-site input, not a constant, and it has to reach the argv.
"""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

import pytest

from osm_polygon_description_tag.workflow import grid_operator
from osm_polygon_description_tag.workflow.grid_policy import PolicyDecision, PolicyVerdict
from osm_polygon_description_tag.workflow.grid_scheduler import SchedulerError

SHARD = "region.parquet"


def _bundle() -> grid_operator.JobBundle:
    return grid_operator.JobBundle("a" * 64, "b" * 64, "c" * 64, "d" * 64, SHARD, "e" * 64, 8, 1)


def _allowed(tmp_path: Path) -> grid_operator.JobPaths:
    paths = grid_operator.job_paths(tmp_path, _bundle())
    paths.root.mkdir(parents=True)
    paths.script.write_text("#!/usr/bin/env bash\n", encoding="utf-8")
    paths.bundle.write_text(
        grid_operator.canonical_json_bytes(_bundle().to_payload()).decode("utf-8"),
        encoding="utf-8",
    )
    (paths.root / grid_operator.JOB_CONFIG_FILENAME).write_text(
        grid_operator.canonical_json_bytes(
            grid_operator._job_config_payload(
                _bundle(), processing_seconds=1200, batch_size=512, walltime_seconds=1800
            )
        ).decode("utf-8"),
        encoding="utf-8",
    )
    return paths


def _verdict() -> PolicyVerdict:
    return PolicyVerdict(decision=PolicyDecision.ALLOWED, reasons=("permitted",), evidence={})


def _plan(tmp_path: Path, **kwargs: object) -> grid_operator.SubmissionPlan:
    return grid_operator.plan_submission(
        _allowed(tmp_path),
        _bundle(),
        policy=_verdict(),
        allowed_root=tmp_path,
        now=datetime(2026, 9, 13, 9, 0, tzinfo=UTC),
        **kwargs,  # type: ignore[arg-type]
    )


def test_no_queue_is_requested_by_default(tmp_path: Path) -> None:
    """Sites that accept the bare form must keep getting it."""
    assert "-q" not in _plan(tmp_path).argv


def test_a_named_queue_reaches_the_scheduler_argv(tmp_path: Path) -> None:
    """Without this the four abaca sites cannot be used at all."""
    argv = _plan(tmp_path, queue="default").argv

    assert "-q" in argv
    assert argv[argv.index("-q") + 1] == "default"


def test_the_queue_is_requested_after_the_job_name(tmp_path: Path) -> None:
    """The whole argv is the operator's record of what was asked for."""
    argv = _plan(tmp_path, queue="default").argv

    assert argv.index("-q") > argv.index("-n")
    assert argv[-1].endswith("job.sh") or argv[-1].endswith("job.sh'")


@pytest.mark.parametrize("queue", ["", "-besteffort", "def\nault", "def\x00ault"])
def test_a_queue_that_could_smuggle_an_option_or_a_control_code_is_refused(
    tmp_path: Path, queue: str
) -> None:
    """Empty, option-like, and control-bearing names are not queue names.

    The argv reaches ``oarsub`` through ``subprocess`` without a shell, so an
    ordinary space or semicolon is only ever data --- it names a queue that
    will not exist. A leading dash is different: it would be read as another
    option entirely.
    """
    with pytest.raises(SchedulerError):
        _plan(tmp_path, queue=queue)


@pytest.mark.parametrize("queue", ["default", "besteffort", "production"])
def test_the_queue_names_these_sites_actually_use_are_accepted(tmp_path: Path, queue: str) -> None:
    argv = _plan(tmp_path, queue=queue).argv

    assert argv[argv.index("-q") + 1] == queue
