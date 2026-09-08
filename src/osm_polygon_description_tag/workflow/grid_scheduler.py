"""Safe OAR scheduler invocation for tiny, single-core Grid'5000 jobs.

Local commands use explicit argument vectors without a shell. The job-script
argument is separately quoted because OAR later evaluates it through the user's
shell. A submission whose outcome cannot be established -- a timeout, a missing
job identifier, an unreadable response -- is reported as
:data:`SubmissionOutcome.AMBIGUOUS`. Ambiguity must never be resolved by
submitting again: a job may well be queued, and a blind retry would breach the
concurrency-one constraint.
"""

import json
import re
import shlex
import shutil
import subprocess
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path
from typing import Final, cast

DEFAULT_COMMAND_TIMEOUT: Final = 120.0
REQUIRED_CORES: Final = 1
MAX_WALLTIME_SECONDS: Final = 30 * 60
_JOB_ID_PATTERN = re.compile(r"(?:OAR_JOB_ID[ \t]*=[ \t]*)?(?P<job_id>[1-9]\d*)")
_ACCOUNT_JOB_ID_PATTERN = re.compile(r"\A[1-9]\d*\Z")
_UNSAFE_CHARACTERS = ("\x00", "\n", "\r")
_TERMINAL_STATES: Final = frozenset({"Terminated", "Error"})
_ACTIVE_STATES: Final = frozenset(
    {
        "Waiting",
        "Hold",
        "toLaunch",
        "toError",
        "toAckReservation",
        "Launching",
        "Running",
        "Suspended",
        "Resuming",
        "Finishing",
    }
)


class SchedulerError(ValueError):
    """Raised when a scheduler request cannot be constructed safely."""


class SubmissionOutcome(StrEnum):
    """What is actually known about a submission attempt."""

    SUBMITTED = "submitted"
    REJECTED = "rejected"
    AMBIGUOUS = "ambiguous"


class JobState(StrEnum):
    """The coarse lifecycle state of one OAR job."""

    ACTIVE = "active"
    TERMINATED = "terminated"
    UNKNOWN = "unknown"


@dataclass(frozen=True, slots=True)
class CommandResult:
    """The complete observable result of one scheduler command."""

    argv: tuple[str, ...]
    returncode: int
    stdout: str
    stderr: str
    timed_out: bool = False


CommandRunner = Callable[[Sequence[str], float], CommandResult]


@dataclass(frozen=True, slots=True)
class SubmissionRequest:
    """One bounded single-core job request."""

    script: Path
    walltime_seconds: int
    cores: int
    name: str
    night_noretry: bool = False
    queue: str | None = None

    def __post_init__(self) -> None:
        _validate_argument(self.name, "job name")
        if self.queue is not None:
            _validate_argument(self.queue, "queue")
        _validate_positive_int(
            self.walltime_seconds, "walltime must be a positive number of seconds"
        )
        if self.walltime_seconds > MAX_WALLTIME_SECONDS:
            raise SchedulerError(f"walltime must not exceed {MAX_WALLTIME_SECONDS} seconds")
        _validate_positive_int(self.cores, "cores must be a positive integer")
        if self.cores != REQUIRED_CORES:
            raise SchedulerError(f"exactly {REQUIRED_CORES} core must be requested")


@dataclass(frozen=True, slots=True)
class SubmissionResult:
    """What is known after attempting one submission."""

    outcome: SubmissionOutcome
    job_id: int | None
    argv: tuple[str, ...]
    detail: str

    @property
    def is_ambiguous(self) -> bool:
        """Return whether the submission's fate is genuinely unknown."""
        return self.outcome is SubmissionOutcome.AMBIGUOUS

    def to_payload(self) -> dict[str, object]:
        return {
            "outcome": str(self.outcome),
            "job_id": self.job_id,
            "argv": list(self.argv),
            "detail": self.detail,
        }


def _validate_positive_int(value: object, message: str) -> None:
    if type(value) is not int or value < 1:
        raise SchedulerError(message)


def _required_text(value: object, label: str) -> str:
    if not isinstance(value, str) or not value:
        raise SchedulerError(f"{label} must be a non-empty string")
    return value


def _validate_argument(value: object, label: str) -> str:
    text = _required_text(value, label)
    if any(character in text for character in _UNSAFE_CHARACTERS):
        raise SchedulerError(f"{label} must not contain control characters")
    if text.startswith("-"):
        raise SchedulerError(f"{label} must not start with an option marker")
    return text


def format_walltime(seconds: int) -> str:
    """Format seconds as the ``H:MM:SS`` walltime OAR expects."""
    if type(seconds) is not int or seconds <= 0:
        raise SchedulerError("walltime must be a positive number of seconds")
    hours, remainder = divmod(seconds, 3600)
    minutes, second = divmod(remainder, 60)
    return f"{hours}:{minutes:02d}:{second:02d}"


def contained_script(script: Path, allowed_root: Path) -> Path:
    """Return ``script`` resolved inside ``allowed_root``, rejecting escapes."""
    root = allowed_root.resolve(strict=False)
    if script.is_symlink():
        raise SchedulerError(f"job script must not be a symlink: {script}")
    resolved = script.resolve(strict=False)
    if not resolved.is_relative_to(root):
        raise SchedulerError(f"job script escapes its allowed root: {script}")
    if not resolved.is_file():
        raise SchedulerError(f"job script is missing: {script}")
    return resolved


def build_oarsub_argv(request: SubmissionRequest, *, allowed_root: Path) -> tuple[str, ...]:
    """Build the exact ``oarsub`` argument vector for one bounded job."""
    script = contained_script(request.script, allowed_root)
    argv = [
        "oarsub",
        "-l",
        f"core={request.cores},walltime={format_walltime(request.walltime_seconds)}",
        "-n",
        request.name,
    ]
    if request.night_noretry:
        argv.extend(("-t", "night=noretry"))
    if request.queue is not None:
        argv.extend(("-q", request.queue))
    # OAR stores this argument as a command later executed by the user's shell.
    argv.append(shlex.quote(str(script)))
    return tuple(argv)


def parse_job_id(text: str) -> int | None:
    """Extract an OAR job identifier from command output, or ``None``."""
    record = _identifier_record(text)
    if record is None:
        return None
    match = _JOB_ID_PATTERN.fullmatch(record)
    return int(match.group("job_id")) if match is not None else None


def _identifier_record(text: str) -> str | None:
    named = [line.strip() for line in text.splitlines() if "OAR_JOB_ID" in line]
    if not named:
        return text.strip()
    return named[0] if len(named) == 1 else None


def parse_account_job_states(text: str) -> tuple[tuple[int, JobState], ...]:
    """Parse the strict JSON job map emitted by ``oarstat -u -J``.

    OAR uses positive job identifiers as the top-level JSON keys and each value
    is a job object containing a ``state`` field. Empty ``{}`` is a valid clear
    account; text tables, arrays, malformed JSON, duplicate keys, missing
    fields, and unknown states are rejected rather than treated as zero jobs.
    """
    account_map = _load_account_job_map(text)
    parsed = tuple(
        _parse_account_job_entry(raw_job_id, raw_record)
        for raw_job_id, raw_record in account_map.items()
    )
    return tuple(sorted(parsed))


def resolve_account_job_name(text: str, job_name: str) -> int | None:
    """Resolve one exact name in a strict account-wide JSON job map.

    ``None`` means the account snapshot contains no such job, so callers must
    leave the intent unresolved. A duplicate exact name is unsafe to resolve
    and raises instead of choosing an arbitrary job.
    """
    requested_name = _validate_argument(job_name, "job name")
    matches: list[int] = []
    for raw_job_id, raw_record in _load_account_job_map(text).items():
        job_id, record = _account_job_record(raw_job_id, raw_record)
        _account_job_state_from_record(record, job_id)
        if _account_job_name(record, job_id) == requested_name:
            matches.append(job_id)
    return _one_job_name_match(matches, requested_name)


def _require_account_job_text(text: object) -> str:
    if not isinstance(text, str):
        raise SchedulerError("account-wide oarstat output must be text")
    return text


def _load_account_job_map(text: object) -> dict[str, object]:
    source = _require_account_job_text(text)
    try:
        payload = json.loads(source, object_pairs_hook=_json_object_pairs)
    except (json.JSONDecodeError, TypeError) as error:
        raise SchedulerError("account-wide oarstat output must be valid JSON") from error
    if not isinstance(payload, dict):
        raise SchedulerError("account-wide oarstat output must be a JSON job map")
    return payload


def _json_object_pairs(pairs: list[tuple[str, object]]) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            raise SchedulerError(f"account-wide oarstat output has duplicate key {key!r}")
        result[key] = value
    return result


def _parse_account_job_entry(raw_job_id: str, raw_record: object) -> tuple[int, JobState]:
    job_id, record = _account_job_record(raw_job_id, raw_record)
    return job_id, _account_job_state_from_record(record, job_id)


def _account_job_record(raw_job_id: str, raw_record: object) -> tuple[int, dict[str, object]]:
    job_id = _parse_account_job_id(raw_job_id)
    if not isinstance(raw_record, dict):
        raise SchedulerError("account-wide oarstat job record must be a JSON object")
    record = cast(dict[str, object], raw_record)
    _validate_record_job_id(record, job_id)
    return job_id, record


def _account_job_state_from_record(record: dict[str, object], job_id: int) -> JobState:
    raw_state = record.get("state")
    if not isinstance(raw_state, str):
        raise SchedulerError(f"account-wide oarstat job {job_id} has no state")
    return _account_job_state(raw_state)


def _account_job_name(record: dict[str, object], job_id: int) -> str | None:
    raw_name = record.get("name")
    if raw_name is None:
        return None
    if not isinstance(raw_name, str) or not raw_name:
        raise SchedulerError(f"account-wide oarstat job {job_id} has no valid name")
    return raw_name


def _one_job_name_match(matches: list[int], job_name: str) -> int | None:
    if len(matches) > 1:
        raise SchedulerError(
            f"account-wide oarstat has multiple jobs named {job_name!r}; do not resubmit"
        )
    return matches[0] if matches else None


def _parse_account_job_id(raw_job_id: str) -> int:
    if not _ACCOUNT_JOB_ID_PATTERN.fullmatch(raw_job_id):
        raise SchedulerError("account-wide oarstat output has an invalid job id")
    return int(raw_job_id)


def _validate_record_job_id(record: dict[str, object], job_id: int) -> None:
    if "Job_Id" not in record:
        return
    record_job_id = record["Job_Id"]
    if type(record_job_id) is not int or record_job_id != job_id:
        raise SchedulerError(f"account-wide oarstat job id does not match key {job_id}")


def _account_job_state(raw_state: str) -> JobState:
    if raw_state in _ACTIVE_STATES:
        return JobState.ACTIVE
    if raw_state in _TERMINAL_STATES:
        return JobState.TERMINATED
    raise SchedulerError(f"account-wide oarstat output has an unknown state: {raw_state}")


def parse_account_job_ids(text: str) -> tuple[int, ...]:
    """Return identifiers from strict account-wide scheduler evidence."""
    return tuple(job_id for job_id, _ in parse_account_job_states(text))


def account_active_job_count(text: str) -> int:
    """Count active jobs in strict account-wide scheduler evidence."""
    return sum(state is JobState.ACTIVE for _, state in parse_account_job_states(text))


def run_command(argv: Sequence[str], timeout: float) -> CommandResult:
    """Run one scheduler command without a shell, capturing its output."""
    executable = shutil.which(argv[0])
    if executable is None:
        raise SchedulerError(f"scheduler executable is not available: {argv[0]}")
    try:
        completed = subprocess.run(  # noqa: S603
            [executable, *argv[1:]],
            capture_output=True,
            text=True,
            timeout=timeout,
            check=False,
        )
    except subprocess.TimeoutExpired:
        return CommandResult(tuple(argv), returncode=-1, stdout="", stderr="", timed_out=True)
    return CommandResult(
        tuple(argv),
        returncode=completed.returncode,
        stdout=completed.stdout,
        stderr=completed.stderr,
    )


def submit(
    request: SubmissionRequest,
    *,
    allowed_root: Path,
    runner: CommandRunner = run_command,
    timeout: float = DEFAULT_COMMAND_TIMEOUT,
) -> SubmissionResult:
    """Submit one job, reporting ambiguity rather than guessing."""
    argv = build_oarsub_argv(request, allowed_root=allowed_root)
    result = runner(argv, timeout)
    if result.timed_out:
        return SubmissionResult(
            SubmissionOutcome.AMBIGUOUS,
            None,
            argv,
            "oarsub timed out; a job may or may not be queued, so do not resubmit",
        )
    job_id = parse_job_id(result.stdout)
    if result.returncode != 0:
        return _failed_submission(argv, result, job_id)
    if job_id is None:
        return SubmissionResult(
            SubmissionOutcome.AMBIGUOUS,
            None,
            argv,
            "oarsub reported success without a job identifier; reconcile before resubmitting",
        )
    return SubmissionResult(SubmissionOutcome.SUBMITTED, job_id, argv, "job submitted")


def _failed_submission(
    argv: tuple[str, ...], result: CommandResult, job_id: int | None
) -> SubmissionResult:
    if job_id is not None:
        return SubmissionResult(
            SubmissionOutcome.AMBIGUOUS,
            job_id,
            argv,
            f"oarsub exited {result.returncode} but reported job {job_id}; reconcile it",
        )
    return SubmissionResult(
        SubmissionOutcome.REJECTED,
        None,
        argv,
        f"oarsub exited {result.returncode}: {result.stderr.strip() or 'no diagnostic'}",
    )


def job_state(
    job_id: int,
    *,
    runner: CommandRunner = run_command,
    timeout: float = DEFAULT_COMMAND_TIMEOUT,
) -> tuple[JobState, str]:
    """Report one job's coarse state, defaulting to unknown."""
    if type(job_id) is not int or job_id <= 0:
        raise SchedulerError("job id must be a positive integer")
    argv = ("oarstat", "-j", str(job_id), "-s")
    result = runner(argv, timeout)
    if result.timed_out:
        return JobState.UNKNOWN, "oarstat timed out"
    if result.returncode != 0:
        return JobState.UNKNOWN, f"oarstat exited {result.returncode}"
    return _state_from_output(result.stdout)


def _state_from_output(text: str) -> tuple[JobState, str]:
    state = text.split(":")[-1].strip() if ":" in text else text.strip()
    if state in _ACTIVE_STATES:
        return JobState.ACTIVE, state
    if state in _TERMINAL_STATES:
        return JobState.TERMINATED, state
    return JobState.UNKNOWN, state or "no state reported"


__all__ = [
    "DEFAULT_COMMAND_TIMEOUT",
    "MAX_WALLTIME_SECONDS",
    "REQUIRED_CORES",
    "CommandResult",
    "CommandRunner",
    "JobState",
    "SchedulerError",
    "SubmissionOutcome",
    "SubmissionRequest",
    "SubmissionResult",
    "account_active_job_count",
    "build_oarsub_argv",
    "contained_script",
    "format_walltime",
    "job_state",
    "parse_account_job_ids",
    "parse_account_job_states",
    "parse_job_id",
    "resolve_account_job_name",
    "run_command",
    "submit",
]
