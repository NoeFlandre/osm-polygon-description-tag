"""Fail-closed Grid'5000 usage-policy and quota preflight.

Public Grid'5000 documentation does not define a stable ``usagepolicycheck``
JSON schema, nor an exit-code contract that means "your quota permits this
submission". This module therefore treats a zero exit status as *insufficient*
evidence on its own: anything it cannot positively interpret becomes
:data:`PolicyDecision.UNKNOWN`, which callers must refuse to submit under.

Only fields actually observed in a real capture are read (``start_time``,
``stop_time``, ``jobs``, ``total_jobs``, ``limits``). No "remaining quota" or
"approved" field is invented, because none is documented.

The conservative default submission window is outside weekday daytime in
Europe/Paris, where the published policy imposes the interactive-use rules that
this project cannot verify programmatically. Daytime submission stays available
but must be enabled explicitly by an operator who has confirmed their own
accounting.
"""

import json
import re
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import UTC, datetime, time, timedelta
from enum import IntEnum, StrEnum
from pathlib import PurePosixPath
from typing import Final, cast
from zoneinfo import ZoneInfo

GRID5000_TIMEZONE: Final = ZoneInfo("Europe/Paris")
DAYTIME_START: Final = time(9, 0)
DAYTIME_END: Final = time(19, 0)
MAX_WALLTIME_SECONDS: Final = 30 * 60
MAX_PROCESSING_SECONDS: Final = 20 * 60
REQUIRED_CORES: Final = 1
MAX_CONCURRENT_JOBS: Final = 1
MAX_EVIDENCE_AGE_SECONDS: Final = 5 * 60
_OBSERVED_KEYS: Final = frozenset({"start_time", "stop_time", "jobs", "total_jobs", "limits"})
_QUOTA_LINE = re.compile(
    r"^(?P<filesystem>\S+)\s+"
    r"(?P<used>\d+)\*?\s+(?P<soft>\d+)\s+(?P<hard>\d+)\s+\d+\s+"
    r"(?P<files_used>\d+)\*?\s+(?P<files_soft>\d+)\s+(?P<files_hard>\d+)\s+\d+$"
)


class PolicyDecision(StrEnum):
    """Whether a submission may proceed on the evidence actually available."""

    ALLOWED = "allowed"
    BLOCKED = "blocked"
    UNKNOWN = "unknown"


class GridPolicyError(ValueError):
    """Raised when policy evidence is malformed beyond interpretation."""


@dataclass(frozen=True, slots=True)
class PolicyEvidence:
    """What the preflight commands actually reported, without embellishment."""

    active_job_count: int | None
    home_quota_exceeded: bool | None
    usage_policy_parsed: bool
    notes: tuple[str, ...]
    captured_at: datetime | None = None

    def to_payload(self) -> dict[str, object]:
        return {
            "active_job_count": self.active_job_count,
            "home_quota_exceeded": self.home_quota_exceeded,
            "usage_policy_parsed": self.usage_policy_parsed,
            "notes": list(self.notes),
            "captured_at": None if self.captured_at is None else self.captured_at.isoformat(),
        }


@dataclass(frozen=True, slots=True)
class PolicyVerdict:
    """A decision plus every reason that produced it."""

    decision: PolicyDecision
    reasons: tuple[str, ...]
    evidence: PolicyEvidence

    @property
    def may_submit(self) -> bool:
        """Return whether submission is permitted; unknown is never permitted."""
        return self.decision is PolicyDecision.ALLOWED

    def to_payload(self) -> dict[str, object]:
        return {
            "decision": str(self.decision),
            "may_submit": self.may_submit,
            "reasons": list(self.reasons),
            "evidence": self.evidence.to_payload(),
        }


def is_weekday_daytime(moment: datetime) -> bool:
    """Return whether ``moment`` falls in weekday 09:00-19:00 Europe/Paris."""
    if moment.tzinfo is None:
        raise GridPolicyError("policy time must be timezone aware")
    local = moment.astimezone(GRID5000_TIMEZONE)
    if local.weekday() >= 5:
        return False
    return DAYTIME_START <= local.time() < DAYTIME_END


def policy_evidence_is_fresh(
    moment: datetime,
    captured_at: datetime,
    *,
    max_age_seconds: int = MAX_EVIDENCE_AGE_SECONDS,
) -> bool:
    """Return whether a timezone-aware capture is no older than the bound."""
    _validate_aware_time(moment, "policy evaluation time")
    _validate_aware_time(captured_at, "policy evidence time")
    if type(max_age_seconds) is not int or max_age_seconds < 1:
        raise GridPolicyError("maximum evidence age must be a positive integer")
    age = (moment.astimezone(UTC) - captured_at.astimezone(UTC)).total_seconds()
    return 0 <= age <= max_age_seconds


def _validate_aware_time(value: datetime, label: str) -> None:
    if not isinstance(value, datetime) or value.tzinfo is None:
        raise GridPolicyError(f"{label} must be timezone aware")


def parse_usage_policy_json(text: str) -> tuple[int | None, tuple[str, ...]]:
    """Read the historical count from ``usagepolicycheck -t --sites SITE --json``.

    Returns the reported job count when the observed schema is present, and
    ``None`` whenever the output does not match what has actually been seen.
    No quota headroom is inferred, because the tool does not report any.
    """
    payload, notes = _observed_policy_object(text)
    if payload is None:
        return None, notes
    shape_notes = _usage_policy_shape_notes(payload)
    if shape_notes:
        return None, shape_notes
    return _historical_job_count(payload)


def _historical_job_count(payload: Mapping[str, object]) -> tuple[int | None, tuple[str, ...]]:
    total = payload["total_jobs"]
    if type(total) is not int or total < 0:
        return None, ("usage policy total_jobs is not a non-negative integer",)
    jobs = cast(list[object], payload["jobs"])  # pragma: no mutate - static narrowing
    if len(jobs) != total:
        return None, ("usage policy total_jobs does not match jobs",)
    return total, ()


def _observed_policy_object(text: str) -> tuple[dict[str, object] | None, tuple[str, ...]]:
    if not isinstance(text, str):
        return None, ("usage policy output must be text",)
    try:
        payload = json.loads(text)
    except json.JSONDecodeError as error:
        return None, (f"usage policy output is not JSON: {error}",)
    if not isinstance(payload, dict):
        return None, ("usage policy output is not a JSON object",)
    if not _OBSERVED_KEYS.issubset(payload):
        missing = sorted(_OBSERVED_KEYS - set(payload))
        return None, (f"usage policy output lacks observed keys: {', '.join(missing)}",)
    return payload, ()


def _usage_policy_shape_notes(payload: Mapping[str, object]) -> tuple[str, ...]:
    notes: list[str] = []
    for name in ("start_time", "stop_time"):
        if type(payload[name]) is not int:
            notes.append(f"usage policy {name} must be an integer")
    notes.extend(_job_list_notes(payload["jobs"]))
    if not isinstance(payload["limits"], Mapping):
        notes.append("usage policy limits must be an object")
    return tuple(notes)


def _job_list_notes(jobs: object) -> tuple[str, ...]:
    if not isinstance(jobs, list):
        return ("usage policy jobs must be a list",)
    if any(not isinstance(job, Mapping) for job in jobs):
        return ("usage policy jobs must contain objects",)
    return ()


def parse_home_quota(text: str) -> tuple[bool | None, tuple[str, ...]]:
    """Read whether any NFS home quota is exceeded from ``quota -p -w`` output.

    ``quota -l`` is deliberately not supported: it excludes NFS home storage,
    which is exactly the quota that matters here.
    """
    states = _quota_states(text)
    if not states:
        return None, ("home quota output could not be parsed",)
    state, filesystem = max(states, key=lambda item: item[0])
    if state is _QuotaState.HARD:
        return True, (f"home quota hard limit reached on {filesystem}",)
    exceeded = state is _QuotaState.SOFT
    return exceeded, _soft_limit_notes(exceeded)


def _is_home_filesystem(filesystem: str) -> bool:
    return "home" in PurePosixPath(filesystem.rsplit(":", 1)[-1]).parts


def _soft_limit_notes(exceeded: bool) -> tuple[str, ...]:
    return ("home quota soft limit reached",) if exceeded else ()


def _quota_states(text: str) -> list[tuple["_QuotaState", str]] | None:
    states: list[tuple[_QuotaState, str]] = []
    for line in text.splitlines():
        fields = line.split()
        if not fields or not _is_home_filesystem(fields[0]):
            continue
        match = _QUOTA_LINE.fullmatch(line.strip())
        if match is None:
            return None
        states.append((_quota_line_state(match), fields[0]))
    return states


class _QuotaState(IntEnum):
    CLEAR = 0
    SOFT = 1
    HARD = 2


def _quota_line_state(match: re.Match[str]) -> _QuotaState:
    blocks = _limit_state(
        int(match.group("used")), int(match.group("soft")), int(match.group("hard"))
    )
    files = _limit_state(
        int(match.group("files_used")),
        int(match.group("files_soft")),
        int(match.group("files_hard")),
    )
    return max(blocks, files)


def _limit_state(used: int, soft: int, hard: int) -> _QuotaState:
    if hard and used >= hard:
        return _QuotaState.HARD
    if soft and used >= soft:
        return _QuotaState.SOFT
    return _QuotaState.CLEAR


def _window_reasons(moment: datetime, allow_daytime: bool) -> list[str]:
    if not is_weekday_daytime(moment):
        return []
    if allow_daytime:
        return []
    return [
        "weekday daytime in Europe/Paris; daytime accounting is not verifiable "
        "from public documentation, so submission is blocked by default"
    ]


def _job_reasons(active_jobs: int | None) -> tuple[list[str], list[str]]:
    if active_jobs is None:
        return [], ["account-wide active job count is unknown"]
    if active_jobs >= MAX_CONCURRENT_JOBS:
        return [f"{active_jobs} job(s) already active; concurrency is limited to 1"], []
    return [], []


def _quota_reasons(quota_exceeded: bool | None) -> tuple[list[str], list[str]]:
    if quota_exceeded is None:
        return [], ["home quota state is unknown"]
    if quota_exceeded:
        return ["home storage quota is exhausted"], []
    return [], []


def evaluate_policy(
    *,
    moment: datetime,
    usage_policy_output: str | None,
    home_quota_output: str | None,
    walltime_seconds: int,
    cores: int,
    allow_daytime: bool = False,
    account_job_count: int | None = None,
    evidence_captured_at: datetime | None = None,
    require_fresh_evidence: bool = False,
) -> PolicyVerdict:
    """Decide whether one tiny job may be submitted, failing closed on doubt."""
    _validate_optional_output(usage_policy_output, "usage policy output")
    _validate_optional_output(home_quota_output, "home quota output")
    _validate_account_job_count(account_job_count)
    blocking: list[str] = list(_window_reasons(moment, allow_daytime))
    blocking.extend(_walltime_reasons(walltime_seconds, moment))
    if type(cores) is not int or cores != REQUIRED_CORES:
        blocking.append(f"exactly {REQUIRED_CORES} core must be requested, not {cores}")
    evidence = _read_evidence(
        usage_policy_output, home_quota_output, account_job_count, evidence_captured_at
    )
    unknowns = _freshness_reasons(moment, evidence_captured_at, require_fresh_evidence)
    unknowns.extend(_usage_evidence_reasons(usage_policy_output, evidence.usage_policy_parsed))
    for reasons, unknown_reasons in (
        _job_reasons(account_job_count),
        _quota_reasons(evidence.home_quota_exceeded),
    ):
        blocking.extend(reasons)
        unknowns.extend(unknown_reasons)
    return _verdict(blocking, unknowns, evidence)


def _freshness_reasons(moment: datetime, captured_at: datetime | None, required: bool) -> list[str]:
    if not required:
        return []
    if captured_at is None:
        return ["policy evidence freshness is unknown"]
    if not policy_evidence_is_fresh(moment, captured_at):
        return ["policy evidence freshness is stale or from the future"]
    return []


def _usage_evidence_reasons(output: str | None, parsed: bool) -> list[str]:
    if output is None:
        return ["usage policy evidence is unknown"]
    if not parsed:
        return ["usage policy evidence is invalid"]
    return []


def _read_evidence(
    usage_policy_output: str | None,
    home_quota_output: str | None,
    account_job_count: int | None,
    captured_at: datetime | None,
) -> PolicyEvidence:
    usage_policy_count, policy_notes = (
        (None, ("usage policy was not consulted",))
        if usage_policy_output is None
        else parse_usage_policy_json(usage_policy_output)
    )
    quota_exceeded, quota_notes = (
        (None, ("home quota was not consulted",))
        if home_quota_output is None
        else parse_home_quota(home_quota_output)
    )
    return PolicyEvidence(
        active_job_count=account_job_count,
        home_quota_exceeded=quota_exceeded,
        usage_policy_parsed=usage_policy_count is not None,
        notes=tuple(policy_notes) + tuple(quota_notes),
        captured_at=captured_at,
    )


def _validate_optional_output(value: object, label: str) -> None:
    if value is not None and not isinstance(value, str):
        raise GridPolicyError(f"{label} must be text or null")


def _validate_account_job_count(value: object) -> None:
    if value is not None and (type(value) is not int or value < 0):
        raise GridPolicyError(
            "account-wide active job count must be a non-negative integer or null"
        )


def _walltime_reasons(walltime_seconds: int, moment: datetime) -> list[str]:
    if type(walltime_seconds) is not int or walltime_seconds <= 0:
        return ["walltime must be a positive number of seconds"]
    if walltime_seconds > MAX_WALLTIME_SECONDS:
        return [f"walltime {walltime_seconds}s exceeds the {MAX_WALLTIME_SECONDS}s limit"]
    last_instant = moment.astimezone(UTC) + timedelta(seconds=walltime_seconds, microseconds=-1)
    if is_weekday_daytime(moment) != is_weekday_daytime(last_instant):
        return ["job walltime crosses a Europe/Paris day/night boundary"]
    return []


def _verdict(blocking: list[str], unknowns: list[str], evidence: PolicyEvidence) -> PolicyVerdict:
    if blocking:
        return PolicyVerdict(PolicyDecision.BLOCKED, tuple(blocking), evidence)
    if unknowns:
        return PolicyVerdict(PolicyDecision.UNKNOWN, tuple(unknowns), evidence)
    return PolicyVerdict(
        PolicyDecision.ALLOWED,
        ("permitted window, one core, bounded walltime, no active job, quota available",),
        evidence,
    )


__all__ = [
    "DAYTIME_END",
    "DAYTIME_START",
    "GRID5000_TIMEZONE",
    "MAX_CONCURRENT_JOBS",
    "MAX_EVIDENCE_AGE_SECONDS",
    "MAX_PROCESSING_SECONDS",
    "MAX_WALLTIME_SECONDS",
    "REQUIRED_CORES",
    "GridPolicyError",
    "PolicyDecision",
    "PolicyEvidence",
    "PolicyVerdict",
    "evaluate_policy",
    "is_weekday_daytime",
    "parse_home_quota",
    "parse_usage_policy_json",
    "policy_evidence_is_fresh",
]
