"""Safe OAR invocation, ambiguity handling, and fake-executable behaviour."""

import os
import shlex
import subprocess
from collections.abc import Sequence
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
    contained_script,
    format_walltime,
    job_state,
    parse_account_job_ids,
    parse_account_job_states,
    parse_job_id,
    resolve_account_job_name,
    run_command,
    submit,
)


@pytest.fixture
def script(tmp_path: Path) -> Path:
    path = tmp_path / "jobs" / "job.sh"
    path.parent.mkdir(parents=True)
    path.write_text("#!/usr/bin/env bash\ntrue\n", encoding="utf-8")
    return path


def _request(script: Path, **overrides: object) -> SubmissionRequest:
    defaults: dict[str, object] = {
        "script": script,
        "walltime_seconds": 1800,
        "cores": 1,
        "name": "lang-abc",
    }
    return SubmissionRequest(**{**defaults, **overrides})  # type: ignore[arg-type]


def _runner(result: CommandResult) -> object:
    calls: list[tuple[tuple[str, ...], float]] = []

    def _run(argv: Sequence[str], timeout: float) -> CommandResult:
        calls.append((tuple(argv), timeout))
        return CommandResult(
            tuple(argv), result.returncode, result.stdout, result.stderr, result.timed_out
        )

    _run.calls = calls  # type: ignore[attr-defined]
    return _run


def _fake_executable(directory: Path, name: str, body: str) -> None:
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / name
    path.write_text(f"#!/usr/bin/env bash\n{body}\n", encoding="utf-8")
    path.chmod(0o700)


@pytest.mark.parametrize(
    ("seconds", "expected"),
    [(1800, "0:30:00"), (60, "0:01:00"), (3661, "1:01:01"), (1, "0:00:01")],
)
def test_walltime_is_formatted_for_oar(seconds: int, expected: str) -> None:
    assert format_walltime(seconds) == expected


@pytest.mark.parametrize("seconds", [0, -1, 1.5, True])
def test_an_invalid_walltime_is_rejected(seconds: object) -> None:
    with pytest.raises(SchedulerError, match="positive number of seconds"):
        format_walltime(seconds)  # type: ignore[arg-type]


def test_the_submission_argv_is_explicit_and_bounded(script: Path, tmp_path: Path) -> None:
    argv = build_oarsub_argv(_request(script), allowed_root=tmp_path)

    assert argv[:5] == ("oarsub", "-l", "core=1,walltime=0:30:00", "-n", "lang-abc")
    assert argv[-1] == str(script.resolve())
    assert "night=noretry" not in argv


def test_night_noretry_and_queue_are_opt_in(script: Path, tmp_path: Path) -> None:
    argv = build_oarsub_argv(
        _request(script, night_noretry=True, queue="besteffort"), allowed_root=tmp_path
    )

    assert "-t" in argv
    assert "night=noretry" in argv
    assert argv[argv.index("-q") + 1] == "besteffort"


@pytest.mark.parametrize(
    ("overrides", "message"),
    [
        ({"name": ""}, "job name must be a non-empty string"),
        ({"name": "a\nb"}, "control characters"),
        ({"name": "--rm-rf"}, "option marker"),
        ({"name": 7}, "job name must be a non-empty string"),
        ({"queue": "-x"}, "option marker"),
        ({"walltime_seconds": 0}, "positive number of seconds"),
        ({"cores": 0}, "cores must be a positive integer"),
        ({"cores": True}, "cores must be a positive integer"),
    ],
)
def test_unsafe_request_fields_are_rejected(
    script: Path, overrides: dict[str, object], message: str
) -> None:
    with pytest.raises(SchedulerError, match=message):
        _request(script, **overrides)


def test_submission_rejects_walltime_above_the_oar_limit(script: Path) -> None:
    with pytest.raises(SchedulerError, match="walltime must not exceed 1800 seconds"):
        _request(script, walltime_seconds=1801)


def test_submission_rejects_requests_for_more_than_one_core(script: Path) -> None:
    with pytest.raises(SchedulerError, match="exactly 1 core"):
        _request(script, cores=2)


def test_a_script_outside_the_allowed_root_is_rejected(script: Path, tmp_path: Path) -> None:
    outside = tmp_path.parent / "elsewhere.sh"
    outside.write_text("#!/usr/bin/env bash\ntrue\n", encoding="utf-8")

    with pytest.raises(SchedulerError, match="escapes its allowed root"):
        contained_script(outside, tmp_path)


def test_a_missing_or_symlinked_script_is_rejected(script: Path, tmp_path: Path) -> None:
    with pytest.raises(SchedulerError, match="job script is missing"):
        contained_script(tmp_path / "jobs" / "absent.sh", tmp_path)

    linked = tmp_path / "jobs" / "linked.sh"
    linked.symlink_to(script)
    with pytest.raises(SchedulerError, match="must not be a symlink"):
        contained_script(linked, tmp_path)


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("OAR_JOB_ID=12345", 12345),
        ("noise\nOAR_JOB_ID = 7\nmore", 7),
        ("notice\r\n OAR_JOB_ID = 8 \r\ncomplete\r\n", 8),
        ("  4242  ", 4242),
        ("no identifier here", None),
        ("", None),
        ("OAR_JOB_ID=0", None),
        ("  0  ", None),
        ("noise\nOAR_JOB_ID=123garbage\nmore", None),
        ("OAR_JOB_ID=123garbage\nOAR_JOB_ID=7", None),
        ("OAR_JOB_ID=123\nOAR_JOB_ID=124", None),
        ("OAR_JOB_ID=123\nOAR_JOB_ID=123", None),
    ],
)
def test_job_identifiers_are_parsed_conservatively(text: str, expected: int | None) -> None:
    assert parse_job_id(text) == expected


def test_oar_command_quotes_the_script_for_its_later_shell_execution(tmp_path: Path) -> None:
    script = tmp_path / "job with spaces; printf injected"
    script.write_text("#!/bin/sh\nprintf 'intended\\n'\n", encoding="utf-8")
    script.chmod(0o700)
    request = SubmissionRequest(script, walltime_seconds=60, cores=1, name="synthetic")

    argv = build_oarsub_argv(request, allowed_root=tmp_path)
    result = subprocess.run(  # noqa: S603 - literal synthetic script in the test directory
        ["/bin/sh", "-c", argv[-1]],
        cwd=tmp_path,
        capture_output=True,
        text=True,
        check=False,
    )

    assert result.returncode == 0, result.stderr
    assert result.stdout == "intended\n"
    assert argv[-1] == shlex.quote(str(script.resolve()))


def test_account_wide_job_states_are_parsed_strictly() -> None:
    account_jobs = (
        '{"123":{"Job_Id":123,"state":"Running"},"124":{"Job_Id":124,"state":"Terminated"}}'
    )
    assert parse_account_job_states(account_jobs) == (
        (123, JobState.ACTIVE),
        (124, JobState.TERMINATED),
    )

    with pytest.raises(SchedulerError, match="account-wide oarstat"):
        parse_account_job_states("Job id User State\n123: Running\n")
    with pytest.raises(SchedulerError, match="duplicate"):
        parse_account_job_states('{"123":{"state":"Running"},"123":{"state":"Waiting"}}')
    with pytest.raises(SchedulerError, match="account-wide oarstat"):
        parse_account_job_states('[{"Job_Id":123,"state":"Running"}]')


def test_account_wide_job_states_reject_inconsistent_json_identity() -> None:
    with pytest.raises(SchedulerError, match="job id"):
        parse_account_job_states('{"123":{"Job_Id":124,"state":"Running"}}')


def test_account_wide_job_states_require_text_input() -> None:
    with pytest.raises(SchedulerError, match="output must be text"):
        parse_account_job_states(None)  # type: ignore[arg-type]


def test_account_wide_job_states_require_object_records() -> None:
    with pytest.raises(SchedulerError, match="job record must be a JSON object"):
        parse_account_job_states('{"123": []}')


@pytest.mark.parametrize(
    "record",
    ['{"123": {}}', '{"123": {"state": 7}}'],
)
def test_account_wide_job_states_require_a_string_state(record: str) -> None:
    with pytest.raises(SchedulerError, match="has no state"):
        parse_account_job_states(record)


def test_account_wide_job_states_reject_invalid_job_keys() -> None:
    with pytest.raises(SchedulerError, match="invalid job id"):
        parse_account_job_states('{"0": {"state": "Running"}}')


def test_account_wide_job_states_reject_unknown_lifecycle_states() -> None:
    with pytest.raises(SchedulerError, match="unknown state"):
        parse_account_job_states('{"123": {"state": "Queued"}}')


def test_account_job_ids_are_extracted_from_strict_account_evidence() -> None:
    account_jobs = '{"124":{"state":"Terminated"},"123":{"state":"Running"}}'

    assert parse_account_job_ids(account_jobs) == (123, 124)


def test_account_active_job_count_ignores_terminal_records() -> None:
    account_jobs = '{"123":{"state":"Running"},"124":{"state":"Terminated"}}'

    assert account_active_job_count(account_jobs) == 1


def test_account_job_name_resolution_requires_one_exact_match() -> None:
    account_jobs = (
        '{"123":{"Job_Id":123,"name":"lang-target", "state":"Terminated"},'
        '"124":{"Job_Id":124,"name":"lang-other", "state":"Running"}}'
    )

    assert resolve_account_job_name(account_jobs, "lang-target") == 123
    assert resolve_account_job_name(account_jobs, "lang-missing") is None

    duplicate = '{"123":{"name":"lang-target","state":"Running"},'
    duplicate += '"124":{"name":"lang-target","state":"Terminated"}}'
    with pytest.raises(SchedulerError, match="multiple"):
        resolve_account_job_name(duplicate, "lang-target")


def test_account_job_name_resolution_ignores_unrelated_null_or_missing_names() -> None:
    account_jobs = (
        '{"123":{"Job_Id":123,"name":"lang-target","state":"Running"},'
        '"124":{"Job_Id":124,"name":null,"state":"Terminated"},'
        '"125":{"Job_Id":125,"state":"Terminated"}}'
    )

    assert resolve_account_job_name(account_jobs, "lang-target") == 123


@pytest.mark.parametrize(
    "payload",
    [
        '{"123":{"name":123,"state":"Running"}}',
        '{"123":{"name":[],"state":"Running"}}',
        '{"123":{"name":"","state":"Running"}}',
    ],
)
def test_account_job_name_resolution_rejects_malformed_names(payload: str) -> None:
    with pytest.raises(SchedulerError, match="name"):
        resolve_account_job_name(payload, "lang-target")


@pytest.mark.parametrize("state", ["Finishing", "Suspended", "Resuming", "toError"])
def test_cleanup_and_transitional_job_states_remain_active(state: str) -> None:
    payload = f'{{"123":{{"state":"{state}"}}}}'

    assert parse_account_job_states(payload) == ((123, JobState.ACTIVE),)


def test_a_successful_submission_reports_its_job_id(script: Path, tmp_path: Path) -> None:
    runner = _runner(CommandResult((), 0, "OAR_JOB_ID=99\n", ""))

    result = submit(_request(script), allowed_root=tmp_path, runner=runner)  # type: ignore[arg-type]

    assert result.outcome is SubmissionOutcome.SUBMITTED
    assert result.job_id == 99
    assert not result.is_ambiguous
    assert result.to_payload()["job_id"] == 99


@pytest.mark.parametrize(
    "output",
    [
        "OAR_JOB_ID=0\r\n",
        "noise\nOAR_JOB_ID=123garbage\n",
        "OAR_JOB_ID=123\nOAR_JOB_ID=124\n",
    ],
)
def test_success_with_an_invalid_job_id_is_ambiguous_and_not_retried(
    script: Path, tmp_path: Path, output: str
) -> None:
    calls: list[tuple[str, ...]] = []

    def runner(argv: Sequence[str], timeout: float) -> CommandResult:
        del timeout
        calls.append(tuple(argv))
        return CommandResult(tuple(argv), 0, output, "")

    result = submit(_request(script), allowed_root=tmp_path, runner=runner)

    assert result.outcome is SubmissionOutcome.AMBIGUOUS
    assert result.job_id is None
    assert "reconcile before resubmitting" in result.detail
    assert len(calls) == 1


def test_a_timeout_is_ambiguous_and_never_a_rejection(script: Path, tmp_path: Path) -> None:
    runner = _runner(CommandResult((), -1, "", "", True))

    result = submit(_request(script), allowed_root=tmp_path, runner=runner)  # type: ignore[arg-type]

    assert result.outcome is SubmissionOutcome.AMBIGUOUS
    assert result.job_id is None
    assert "do not resubmit" in result.detail


def test_success_without_a_job_id_is_ambiguous(script: Path, tmp_path: Path) -> None:
    runner = _runner(CommandResult((), 0, "submitted, probably\n", ""))

    result = submit(_request(script), allowed_root=tmp_path, runner=runner)  # type: ignore[arg-type]

    assert result.outcome is SubmissionOutcome.AMBIGUOUS
    assert "reconcile before resubmitting" in result.detail


def test_a_failure_that_still_names_a_job_is_ambiguous(script: Path, tmp_path: Path) -> None:
    runner = _runner(CommandResult((), 1, "OAR_JOB_ID=55\n", "partial failure"))

    result = submit(_request(script), allowed_root=tmp_path, runner=runner)  # type: ignore[arg-type]

    assert result.outcome is SubmissionOutcome.AMBIGUOUS
    assert result.job_id == 55
    assert "reconcile it" in result.detail


def test_a_clean_failure_is_a_rejection(script: Path, tmp_path: Path) -> None:
    runner = _runner(CommandResult((), 2, "", "quota exceeded\n"))

    result = submit(_request(script), allowed_root=tmp_path, runner=runner)  # type: ignore[arg-type]

    assert result.outcome is SubmissionOutcome.REJECTED
    assert result.job_id is None
    assert "quota exceeded" in result.detail


def test_a_rejection_without_a_diagnostic_still_explains_itself(
    script: Path, tmp_path: Path
) -> None:
    runner = _runner(CommandResult((), 3, "", "   "))

    result = submit(_request(script), allowed_root=tmp_path, runner=runner)  # type: ignore[arg-type]

    assert result.outcome is SubmissionOutcome.REJECTED
    assert "no diagnostic" in result.detail


@pytest.mark.parametrize(
    ("output", "expected"),
    [
        ("Running", JobState.ACTIVE),
        ("job 5: Waiting", JobState.ACTIVE),
        ("Terminated", JobState.TERMINATED),
        ("Error", JobState.TERMINATED),
        ("Bewildered", JobState.UNKNOWN),
        ("", JobState.UNKNOWN),
    ],
)
def test_job_states_are_classified(output: str, expected: JobState) -> None:
    runner = _runner(CommandResult((), 0, output, ""))

    state, _ = job_state(5, runner=runner)  # type: ignore[arg-type]

    assert state is expected


def test_an_unreachable_scheduler_leaves_the_state_unknown() -> None:
    timed_out = _runner(CommandResult((), -1, "", "", True))
    failed = _runner(CommandResult((), 1, "", "boom"))

    assert job_state(5, runner=timed_out)[0] is JobState.UNKNOWN  # type: ignore[arg-type]
    assert job_state(5, runner=failed)[0] is JobState.UNKNOWN  # type: ignore[arg-type]


@pytest.mark.parametrize("job_id", [0, -1, "5", True])
def test_an_invalid_job_id_is_rejected(job_id: object) -> None:
    with pytest.raises(SchedulerError, match="job id must be a positive integer"):
        job_state(job_id)  # type: ignore[arg-type]


def test_a_fake_oarsub_executable_is_invoked_without_a_shell(
    script: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    fake_bin = tmp_path / "bin"
    _fake_executable(fake_bin, "oarsub", 'echo "OAR_JOB_ID=4321"')
    monkeypatch.setenv("PATH", f"{fake_bin}{os.pathsep}{os.environ['PATH']}")

    result = submit(_request(script), allowed_root=tmp_path)

    assert result.outcome is SubmissionOutcome.SUBMITTED
    assert result.job_id == 4321


def test_a_fake_scheduler_failure_is_reported_faithfully(
    script: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    fake_bin = tmp_path / "bin"
    _fake_executable(fake_bin, "oarsub", 'echo "policy refused" >&2\nexit 4')
    monkeypatch.setenv("PATH", f"{fake_bin}{os.pathsep}{os.environ['PATH']}")

    result = submit(_request(script), allowed_root=tmp_path)

    assert result.outcome is SubmissionOutcome.REJECTED
    assert "policy refused" in result.detail


def test_a_fake_oarstat_reports_a_state(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    fake_bin = tmp_path / "bin"
    _fake_executable(fake_bin, "oarstat", 'echo "4321: Running"')
    monkeypatch.setenv("PATH", f"{fake_bin}{os.pathsep}{os.environ['PATH']}")

    state, detail = job_state(4321)

    assert state is JobState.ACTIVE
    assert detail == "Running"


def test_a_missing_executable_is_reported(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setenv("PATH", str(tmp_path / "empty"))

    with pytest.raises(SchedulerError, match="scheduler executable is not available"):
        run_command(["oarsub", "--version"], 5.0)


def test_a_slow_executable_times_out_without_raising(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    fake_bin = tmp_path / "bin"
    _fake_executable(fake_bin, "oarstat", "sleep 5")
    monkeypatch.setenv("PATH", f"{fake_bin}{os.pathsep}{os.environ['PATH']}")

    result = run_command(["oarstat", "-j", "1"], 0.25)

    assert result.timed_out
    assert result.returncode == -1
