"""Regression tests for scheduler command-boundary compatibility."""

from collections.abc import Sequence

import pytest

from osm_polygon_description_tag.workflow.grid_operator import (
    GridOperatorError,
    gather_policy_evidence,
    resolve_job_name,
)
from osm_polygon_description_tag.workflow.grid_scheduler import (
    CommandResult,
    CommandRunner,
    SchedulerError,
    parse_account_job_states,
)


def _scripted_runner(
    results: list[CommandResult],
) -> tuple[CommandRunner, list[tuple[str, ...]]]:
    remaining = iter(results)
    calls: list[tuple[str, ...]] = []

    def run(argv: Sequence[str], timeout: float) -> CommandResult:
        del timeout
        calls.append(tuple(argv))
        return next(remaining)

    return run, calls


@pytest.mark.parametrize("text", ["", "\n", " \n"])
def test_account_parser_keeps_blank_output_strict(text: str) -> None:
    with pytest.raises(SchedulerError, match="valid JSON"):
        parse_account_job_states(text)


def test_policy_evidence_normalizes_successful_empty_account_output() -> None:
    runner, calls = _scripted_runner(
        [
            CommandResult((), 0, "policy", ""),
            CommandResult((), 0, "quota", ""),
            CommandResult((), 0, "", ""),
        ]
    )

    assert gather_policy_evidence("nancy", runner=runner) == (
        "policy",
        "quota",
        "{}",
    )
    assert calls == [
        ("usagepolicycheck", "-t", "--sites", "nancy", "--json"),
        ("quota", "-p", "-w"),
        ("oarstat", "-u", "-J"),
    ]


@pytest.mark.parametrize(
    "result",
    [
        CommandResult((), 1, "", "oarstat failed"),
        CommandResult((), 0, "", "", timed_out=True),
    ],
)
def test_policy_evidence_keeps_failed_empty_account_output_unknown(
    result: CommandResult,
) -> None:
    runner, _ = _scripted_runner(
        [CommandResult((), 0, "policy", ""), CommandResult((), 0, "quota", ""), result]
    )

    assert gather_policy_evidence("nancy", runner=runner) == (
        "policy",
        "quota",
        None,
    )


def test_job_name_resolver_treats_successful_empty_account_output_as_no_match() -> None:
    runner, calls = _scripted_runner([CommandResult((), 0, "", "")])

    assert resolve_job_name("lang-target", runner=runner) is None
    assert calls == [("oarstat", "-u", "-J")]


def test_job_name_resolver_keeps_successful_whitespace_output_strict() -> None:
    runner, _ = _scripted_runner([CommandResult((), 0, " \n", "")])

    with pytest.raises(GridOperatorError, match="valid JSON"):
        resolve_job_name("lang-target", runner=runner)
