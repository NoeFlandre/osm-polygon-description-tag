"""Behavioural checks on the GitHub Actions workflows, read as data."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
import yaml

WORKFLOWS = Path(__file__).resolve().parents[2] / ".github" / "workflows"


def _load(name: str) -> dict[Any, Any]:
    return yaml.safe_load((WORKFLOWS / name).read_text(encoding="utf-8"))


def _jobs() -> list[tuple[str, str, dict[str, Any]]]:
    return [
        (path.name, job_id, job)
        for path in sorted(WORKFLOWS.glob("*.yml"))
        for job_id, job in _load(path.name)["jobs"].items()
    ]


@pytest.mark.parametrize(("workflow", "job_id", "job"), _jobs())
def test_every_job_has_a_timeout(workflow: str, job_id: str, job: dict[str, Any]) -> None:
    timeout = job.get("timeout-minutes")

    assert isinstance(timeout, int), f"{workflow}:{job_id} runs without a timeout"
    assert 0 < timeout <= 360


def test_the_aggregate_check_waits_for_every_other_quality_job() -> None:
    jobs = _load("quality.yml")["jobs"]

    assert set(jobs["ci-ok"]["needs"]) == set(jobs) - {"ci-ok"}
    assert jobs["ci-ok"]["if"] == "always()"


def test_the_aggregate_check_accepts_only_success_and_skipped() -> None:
    script = _load("quality.yml")["jobs"]["ci-ok"]["steps"][0]["run"]

    assert "success|skipped) ;;" in script
    assert "exit 1" in script


def test_a_superseded_pull_request_run_is_cancelled_but_a_push_is_not() -> None:
    concurrency = _load("quality.yml")["concurrency"]

    assert "pull_request.number" in concurrency["group"]
    assert concurrency["cancel-in-progress"] == "${{ github.event_name == 'pull_request' }}"


def test_only_the_pages_deploy_job_may_write_pages() -> None:
    jobs = _load("docs.yml")["jobs"]

    assert jobs["build"]["permissions"].get("pages") != "write"
    assert jobs["deploy"]["permissions"]["pages"] == "write"
