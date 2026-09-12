"""Exact scheduler verdicts, argument vectors, and refusal messages.

Every string here is something an operator acts on. ``ambiguous`` means "a job
may already exist, do not submit again", so the detail that says so must survive
verbatim; the recorded ``argv`` is the evidence of what was actually asked for.
These assert whole values rather than fragments.
"""

import os
from pathlib import Path

import pytest

from osm_polygon_description_tag.workflow.grid_scheduler import (
    CommandResult,
    JobState,
    SchedulerError,
    SubmissionOutcome,
    SubmissionRequest,
    account_active_job_count,
    build_oarsub_argv,
    format_walltime,
    job_state,
    parse_account_job_ids,
    parse_account_job_states,
    resolve_account_job_name,
    run_command,
    submit,
)


def _request(tmp_path: Path) -> SubmissionRequest:
    script = tmp_path / "job.sh"
    script.write_text("#!/bin/bash\n", encoding="utf-8")
    script.chmod(0o700)
    return SubmissionRequest(
        script=script,
        walltime_seconds=1800,
        cores=1,
        name="osm-language-region",
        night_noretry=True,
    )


def _runner(result: CommandResult) -> object:
    def run(argv: tuple[str, ...], timeout: float) -> CommandResult:
        run.seen = (argv, timeout)  # type: ignore[attr-defined]
        return result

    return run


def _ok(stdout: str) -> CommandResult:
    return CommandResult(("oarsub",), returncode=0, stdout=stdout, stderr="")


def test_a_timed_out_submission_is_ambiguous_and_says_not_to_resubmit(tmp_path: Path) -> None:
    timed_out = CommandResult(("oarsub",), returncode=-1, stdout="", stderr="", timed_out=True)

    result = submit(_request(tmp_path), allowed_root=tmp_path, runner=_runner(timed_out))

    assert result.outcome is SubmissionOutcome.AMBIGUOUS
    assert result.job_id is None
    assert result.detail == ("oarsub timed out; a job may or may not be queued, so do not resubmit")


def test_a_success_without_a_job_id_is_ambiguous_and_says_to_reconcile(tmp_path: Path) -> None:
    result = submit(_request(tmp_path), allowed_root=tmp_path, runner=_runner(_ok("no id here")))

    assert result.outcome is SubmissionOutcome.AMBIGUOUS
    assert result.job_id is None
    assert result.detail == (
        "oarsub reported success without a job identifier; reconcile before resubmitting"
    )


def test_a_successful_submission_reports_the_job_and_the_exact_argv(tmp_path: Path) -> None:
    result = submit(
        _request(tmp_path), allowed_root=tmp_path, runner=_runner(_ok("OAR_JOB_ID=6917617"))
    )

    assert result.outcome is SubmissionOutcome.SUBMITTED
    assert result.job_id == 6917617
    assert result.detail == "job submitted"
    assert result.argv == build_oarsub_argv(_request(tmp_path), allowed_root=tmp_path)


def test_a_rejection_without_a_job_id_reports_the_exit_code_and_diagnostic(
    tmp_path: Path,
) -> None:
    failed = CommandResult(("oarsub",), returncode=3, stdout="", stderr="  quota exceeded  ")

    result = submit(_request(tmp_path), allowed_root=tmp_path, runner=_runner(failed))

    assert result.outcome is SubmissionOutcome.REJECTED
    assert result.detail == "oarsub exited 3: quota exceeded"


def test_a_rejection_with_no_diagnostic_says_so_rather_than_showing_nothing(
    tmp_path: Path,
) -> None:
    failed = CommandResult(("oarsub",), returncode=3, stdout="", stderr="   ")

    result = submit(_request(tmp_path), allowed_root=tmp_path, runner=_runner(failed))

    assert result.outcome is SubmissionOutcome.REJECTED
    assert result.detail == "oarsub exited 3: no diagnostic"


def test_a_nonzero_exit_that_still_reported_a_job_is_ambiguous(tmp_path: Path) -> None:
    failed = CommandResult(
        ("oarsub",), returncode=1, stdout="OAR_JOB_ID=42", stderr="partial failure"
    )

    result = submit(_request(tmp_path), allowed_root=tmp_path, runner=_runner(failed))

    assert result.outcome is SubmissionOutcome.AMBIGUOUS
    assert result.job_id == 42
    assert result.detail == "oarsub exited 1 but reported job 42; reconcile it"


@pytest.mark.parametrize("job_id", [0, -1, True, 1.0, "7", None])
def test_a_non_positive_job_id_is_refused_with_its_exact_reason(job_id: object) -> None:
    with pytest.raises(SchedulerError) as caught:
        job_state(job_id)  # type: ignore[arg-type]

    assert str(caught.value) == "job id must be a positive integer"


def test_the_smallest_valid_job_id_is_queried_rather_than_refused() -> None:
    runner = _runner(_ok("state: Running"))

    state, detail = job_state(1, runner=runner)  # type: ignore[arg-type]

    assert state is JobState.ACTIVE
    assert detail == "Running"
    assert runner.seen[0] == ("oarstat", "-j", "1", "-s")  # type: ignore[attr-defined]


def test_a_timed_out_state_query_is_unknown_and_says_it_timed_out() -> None:
    timed_out = CommandResult(("oarstat",), returncode=-1, stdout="", stderr="", timed_out=True)

    state, detail = job_state(7, runner=_runner(timed_out))  # type: ignore[arg-type]

    assert state is JobState.UNKNOWN
    assert detail == "oarstat timed out"


def test_empty_state_output_is_unknown_and_says_no_state_was_reported() -> None:
    state, detail = job_state(7, runner=_runner(_ok("   ")))  # type: ignore[arg-type]

    assert state is JobState.UNKNOWN
    assert detail == "no state reported"


def test_a_state_line_is_read_after_its_last_colon() -> None:
    """OAR prints ``<job>: <state>``, so the state is the trailing field."""
    state, detail = job_state(7, runner=_runner(_ok("7: Terminated")))  # type: ignore[arg-type]

    assert state is JobState.TERMINATED
    assert detail == "Terminated"


def test_a_walltime_that_is_not_a_positive_number_of_seconds_is_refused() -> None:
    with pytest.raises(SchedulerError) as caught:
        format_walltime(0)

    assert str(caught.value) == "walltime must be a positive number of seconds"


@pytest.mark.parametrize("text", [None, 7, b"{}"])
def test_non_text_account_evidence_is_refused_with_its_exact_reason(text: object) -> None:
    with pytest.raises(SchedulerError) as caught:
        parse_account_job_states(text)  # type: ignore[arg-type]

    assert str(caught.value) == "account-wide oarstat output must be text"


def test_a_non_object_account_job_record_is_refused_with_its_exact_reason() -> None:
    with pytest.raises(SchedulerError) as caught:
        parse_account_job_states('{"42": "Running"}')

    assert str(caught.value) == "account-wide oarstat job record must be a JSON object"


@pytest.mark.parametrize("raw_job_id", ["0", "-1", "x", "", "07x"])
def test_an_invalid_account_job_id_is_refused_with_its_exact_reason(raw_job_id: str) -> None:
    with pytest.raises(SchedulerError) as caught:
        parse_account_job_states(f'{{"{raw_job_id}": {{"state": "Running"}}}}')

    assert str(caught.value) == "account-wide oarstat output has an invalid job id"


def test_active_jobs_are_counted_and_terminated_jobs_are_not() -> None:
    text = '{"1": {"state": "Running"}, "2": {"state": "Terminated"}, "3": {"state": "Waiting"}}'

    assert account_active_job_count(text) == 2
    assert parse_account_job_ids(text) == (1, 2, 3)


def test_an_empty_account_is_zero_active_jobs_rather_than_unknown() -> None:
    assert account_active_job_count("{}") == 0
    assert parse_account_job_ids("{}") == ()


def test_a_job_name_is_resolved_only_by_an_exact_match() -> None:
    text = (
        '{"11": {"state": "Running", "name": "osm-language-a"},'
        ' "12": {"state": "Running", "name": "osm-language-b"}}'
    )

    assert resolve_account_job_name(text, "osm-language-b") == 12
    assert resolve_account_job_name(text, "osm-language-c") is None


def test_a_duplicate_job_name_refuses_to_resolve_rather_than_pick_one() -> None:
    text = (
        '{"11": {"state": "Running", "name": "same"}, "12": {"state": "Running", "name": "same"}}'
    )

    with pytest.raises(SchedulerError) as caught:
        resolve_account_job_name(text, "same")

    assert str(caught.value) == (
        "account-wide oarstat has multiple jobs named 'same'; do not resubmit"
    )


@pytest.mark.parametrize(
    ("job_name", "message"),
    [
        ("", "job name must be a non-empty string"),
        ("-oops", "job name must not start with an option marker"),
        ("bad\nname", "job name must not contain control characters"),
    ],
)
def test_an_unsafe_job_name_is_refused_under_its_own_label(job_name: str, message: str) -> None:
    with pytest.raises(SchedulerError) as caught:
        resolve_account_job_name("{}", job_name)

    assert str(caught.value) == message


def test_every_submission_outcome_records_the_argv_that_was_actually_sent(
    tmp_path: Path,
) -> None:
    """``argv`` is the operator's evidence of what was asked for, in every outcome."""
    expected = build_oarsub_argv(_request(tmp_path), allowed_root=tmp_path)
    outcomes = (
        CommandResult(("oarsub",), returncode=-1, stdout="", stderr="", timed_out=True),
        CommandResult(("oarsub",), returncode=0, stdout="no id here", stderr=""),
        CommandResult(("oarsub",), returncode=3, stdout="", stderr="quota exceeded"),
        CommandResult(("oarsub",), returncode=1, stdout="OAR_JOB_ID=42", stderr="partial"),
    )

    for command_result in outcomes:
        result = submit(_request(tmp_path), allowed_root=tmp_path, runner=_runner(command_result))

        assert result.argv == expected


def test_the_caller_timeout_reaches_the_scheduler_command(tmp_path: Path) -> None:
    """A dropped timeout would let one stuck oarsub hold the whole rollout."""
    submit_runner = _runner(_ok("OAR_JOB_ID=1"))
    state_runner = _runner(_ok("1: Running"))

    submit(_request(tmp_path), allowed_root=tmp_path, runner=submit_runner, timeout=12.5)
    job_state(1, runner=state_runner, timeout=3.25)  # type: ignore[arg-type]

    assert submit_runner.seen[1] == 12.5  # type: ignore[attr-defined]
    assert state_runner.seen[1] == 3.25  # type: ignore[attr-defined]


def test_a_state_line_that_carries_a_tool_prefix_is_read_after_its_last_colon() -> None:
    """``oarstat: <job>: <state>`` must still report the state, not the job."""
    state, detail = job_state(7, runner=_runner(_ok("oarstat: 7: Terminated")))  # type: ignore[arg-type]

    assert state is JobState.TERMINATED
    assert detail == "Terminated"


def test_a_state_line_without_a_space_after_its_colon_is_still_read() -> None:
    """The separator is the colon; the space around it is not part of the contract."""
    state, detail = job_state(7, runner=_runner(_ok("7:Running")))  # type: ignore[arg-type]

    assert state is JobState.ACTIVE
    assert detail == "Running"


def test_an_account_job_without_a_state_names_the_job_it_belongs_to() -> None:
    """The operator has to find that job among every job on the account."""
    with pytest.raises(SchedulerError) as caught:
        parse_account_job_states('{"42": {"walltime": 1800}}')

    assert str(caught.value) == "account-wide oarstat job 42 has no state"


def test_an_account_job_with_an_unusable_name_names_the_job_it_belongs_to() -> None:
    """Resolving by name must say which job carried the unusable name."""
    with pytest.raises(SchedulerError) as caught:
        resolve_account_job_name('{"42": {"state": "Running", "name": ""}}', "wanted")

    assert str(caught.value) == "account-wide oarstat job 42 has no valid name"


def _fake_bin(tmp_path: Path, name: str, body: str) -> Path:
    directory = tmp_path / "bin"
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / name
    path.write_text(f"#!/usr/bin/env bash\n{body}\n", encoding="utf-8")
    path.chmod(0o700)
    return directory


def test_a_missing_scheduler_executable_names_the_command_that_is_missing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The operator installs the named binary; naming an argument instead is useless."""
    monkeypatch.setenv("PATH", str(tmp_path / "empty"))

    with pytest.raises(SchedulerError) as caught:
        run_command(["oarsub", "--version"], 5.0)

    assert str(caught.value) == "scheduler executable is not available: oarsub"


def test_every_argument_after_the_command_reaches_the_executable(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Dropping the first argument would silently change what was submitted."""
    directory = _fake_bin(tmp_path, "oarstat", 'printf "%s\\n" "$@"')
    monkeypatch.setenv("PATH", f"{directory}{os.pathsep}{os.environ['PATH']}")

    result = run_command(["oarstat", "-j", "7", "-s"], 10.0)

    assert result.stdout == "-j\n7\n-s\n"
    assert result.argv == ("oarstat", "-j", "7", "-s")
    assert result.returncode == 0
    assert result.stderr == ""
    assert result.timed_out is False


def test_a_timed_out_command_reports_the_argv_and_no_captured_output(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Nothing was read, so reporting anything but empty output would be invented."""
    directory = _fake_bin(tmp_path, "oarstat", "sleep 5")
    monkeypatch.setenv("PATH", f"{directory}{os.pathsep}{os.environ['PATH']}")

    result = run_command(["oarstat", "-j", "1"], 0.25)

    assert result.argv == ("oarstat", "-j", "1")
    assert result.returncode == -1
    assert result.stdout == ""
    assert result.stderr == ""
    assert result.timed_out is True
