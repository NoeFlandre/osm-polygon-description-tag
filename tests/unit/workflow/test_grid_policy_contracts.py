"""Exact fail-closed verdicts and reasons of the Grid'5000 usage policy guard.

Each reason is what an operator reads before deciding whether to submit, and
each is asserted whole. Two properties are load-bearing and get their own
tests: the day/night window is always Europe/Paris regardless of the operator's
own machine clock, and anything not positively interpreted stays ``unknown``.
"""

import os
import time
from datetime import UTC, datetime, timedelta
from zoneinfo import ZoneInfo

import pytest

from osm_polygon_description_tag.workflow.grid_policy import (
    GridPolicyError,
    PolicyDecision,
    evaluate_policy,
    is_weekday_daytime,
    parse_home_quota,
    parse_usage_policy_json,
    policy_evidence_is_fresh,
)

_PARIS = ZoneInfo("Europe/Paris")
_NIGHT = datetime(2026, 9, 15, 22, 0, tzinfo=_PARIS)
_DAY = datetime(2026, 9, 15, 12, 0, tzinfo=_PARIS)
_USAGE_JSON = '{"start_time": 1, "stop_time": 2, "jobs": [], "total_jobs": 0, "limits": {}}'
_QUOTA_CLEAR = (
    "Disk quotas for user nflandre (uid 22441): \n"
    "     Filesystem  blocks   quota   limit   grace   files   quota   limit   grace\n"
    "nfs:/export/home 100  25000000  100000000       0   10   0   10000000       0"
)


def test_the_daytime_window_is_evaluated_in_paris_not_in_the_local_timezone(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An operator whose laptop is set to UTC must still get the Paris window.

    22:00 Paris is 20:00 UTC: reading the instant in the machine's own zone
    would call a blocked-free night hour a daytime hour in some zones, and the
    reverse in others.
    """
    monkeypatch.setitem(os.environ, "TZ", "Pacific/Auckland")
    time.tzset()
    try:
        assert not is_weekday_daytime(_NIGHT)
        assert is_weekday_daytime(_DAY)
        assert is_weekday_daytime(_DAY.astimezone(UTC))
    finally:
        monkeypatch.undo()
        time.tzset()


def test_a_naive_policy_time_is_refused_with_its_exact_reason() -> None:
    with pytest.raises(GridPolicyError) as caught:
        is_weekday_daytime(datetime(2026, 9, 15, 22, 0))

    assert str(caught.value) == "policy time must be timezone aware"


def test_a_weekend_daytime_hour_is_not_the_blocked_weekday_window() -> None:
    saturday_noon = datetime(2026, 9, 12, 12, 0, tzinfo=_PARIS)

    assert not is_weekday_daytime(saturday_noon)


@pytest.mark.parametrize(
    ("moment", "expected"),
    [
        (datetime(2026, 9, 15, 8, 59, 59, tzinfo=_PARIS), False),
        (datetime(2026, 9, 15, 9, 0, tzinfo=_PARIS), True),
        (datetime(2026, 9, 15, 18, 59, 59, tzinfo=_PARIS), True),
        (datetime(2026, 9, 15, 19, 0, tzinfo=_PARIS), False),
    ],
)
def test_the_window_boundaries_are_inclusive_at_nine_and_exclusive_at_nineteen(
    moment: datetime, expected: bool
) -> None:
    assert is_weekday_daytime(moment) is expected


def test_weekday_daytime_is_blocked_by_default_with_its_full_reason() -> None:
    verdict = evaluate_policy(
        moment=_DAY,
        usage_policy_output=_USAGE_JSON,
        home_quota_output=_QUOTA_CLEAR,
        walltime_seconds=1800,
        cores=1,
        account_job_count=0,
    )

    assert verdict.decision is PolicyDecision.BLOCKED
    assert verdict.reasons == (
        "weekday daytime in Europe/Paris; daytime accounting is not verifiable "
        "from public documentation, so submission is blocked by default",
    )


def test_a_night_submission_with_complete_evidence_is_allowed() -> None:
    verdict = evaluate_policy(
        moment=_NIGHT,
        usage_policy_output=_USAGE_JSON,
        home_quota_output=_QUOTA_CLEAR,
        walltime_seconds=1800,
        cores=1,
        account_job_count=0,
    )

    assert verdict.decision is PolicyDecision.ALLOWED
    assert verdict.reasons == (
        "permitted window, one core, bounded walltime, no active job, quota available",
    )


def test_a_walltime_crossing_the_boundary_is_blocked_with_its_exact_reason() -> None:
    verdict = evaluate_policy(
        moment=datetime(2026, 9, 15, 18, 50, tzinfo=_PARIS),
        usage_policy_output=_USAGE_JSON,
        home_quota_output=_QUOTA_CLEAR,
        walltime_seconds=1800,
        cores=1,
        allow_daytime=True,
        account_job_count=0,
    )

    assert verdict.decision is PolicyDecision.BLOCKED
    assert "job walltime crosses a Europe/Paris day/night boundary" in verdict.reasons


def test_a_walltime_ending_exactly_at_the_boundary_is_allowed() -> None:
    """The last instant is one microsecond before the end, so this must fit."""
    verdict = evaluate_policy(
        moment=datetime(2026, 9, 15, 18, 30, tzinfo=_PARIS),
        usage_policy_output=_USAGE_JSON,
        home_quota_output=_QUOTA_CLEAR,
        walltime_seconds=1800,
        cores=1,
        allow_daytime=True,
        account_job_count=0,
    )

    assert verdict.decision is PolicyDecision.ALLOWED


@pytest.mark.parametrize("walltime", [0, -1, True, 1.0, "60"])
def test_a_non_positive_walltime_is_blocked_with_its_exact_reason(walltime: object) -> None:
    verdict = evaluate_policy(
        moment=_NIGHT,
        usage_policy_output=_USAGE_JSON,
        home_quota_output=_QUOTA_CLEAR,
        walltime_seconds=walltime,  # type: ignore[arg-type]
        cores=1,
        account_job_count=0,
    )

    assert verdict.decision is PolicyDecision.BLOCKED
    assert "walltime must be a positive number of seconds" in verdict.reasons


def test_the_smallest_positive_walltime_is_not_refused_for_being_non_positive() -> None:
    verdict = evaluate_policy(
        moment=_NIGHT,
        usage_policy_output=_USAGE_JSON,
        home_quota_output=_QUOTA_CLEAR,
        walltime_seconds=1,
        cores=1,
        account_job_count=0,
    )

    assert "walltime must be a positive number of seconds" not in verdict.reasons


def test_unconsulted_evidence_is_unknown_and_names_what_was_not_consulted() -> None:
    verdict = evaluate_policy(
        moment=_NIGHT,
        usage_policy_output=None,
        home_quota_output=None,
        walltime_seconds=1800,
        cores=1,
        account_job_count=0,
    )

    assert verdict.decision is PolicyDecision.UNKNOWN
    assert "usage policy was not consulted" in verdict.evidence.notes
    assert "home quota was not consulted" in verdict.evidence.notes
    assert "usage policy evidence is unknown" in verdict.reasons


def test_missing_evidence_freshness_is_unknown_when_freshness_is_required() -> None:
    verdict = evaluate_policy(
        moment=_NIGHT,
        usage_policy_output=_USAGE_JSON,
        home_quota_output=_QUOTA_CLEAR,
        walltime_seconds=1800,
        cores=1,
        account_job_count=0,
        require_fresh_evidence=True,
    )

    assert verdict.decision is PolicyDecision.UNKNOWN
    assert "policy evidence freshness is unknown" in verdict.reasons


def test_stale_or_future_evidence_is_unknown_with_its_exact_reason() -> None:
    verdict = evaluate_policy(
        moment=_NIGHT,
        usage_policy_output=_USAGE_JSON,
        home_quota_output=_QUOTA_CLEAR,
        walltime_seconds=1800,
        cores=1,
        account_job_count=0,
        evidence_captured_at=_NIGHT + timedelta(hours=1),
        require_fresh_evidence=True,
    )

    assert verdict.decision is PolicyDecision.UNKNOWN
    assert "policy evidence freshness is stale or from the future" in verdict.reasons


@pytest.mark.parametrize(
    ("moment", "captured_at", "label"),
    [
        (datetime(2026, 9, 15, 22, 0), _NIGHT, "policy evaluation time"),
        (_NIGHT, datetime(2026, 9, 15, 22, 0), "policy evidence time"),
    ],
)
def test_a_naive_freshness_argument_is_refused_under_its_own_label(
    moment: datetime, captured_at: datetime, label: str
) -> None:
    with pytest.raises(GridPolicyError) as caught:
        policy_evidence_is_fresh(moment, captured_at)

    assert str(caught.value) == f"{label} must be timezone aware"


def test_evidence_captured_one_second_ago_is_fresh_at_the_minimum_bound() -> None:
    assert policy_evidence_is_fresh(_NIGHT, _NIGHT - timedelta(seconds=1), max_age_seconds=1)
    assert not policy_evidence_is_fresh(_NIGHT, _NIGHT - timedelta(seconds=2), max_age_seconds=1)


@pytest.mark.parametrize(
    ("output", "label"),
    [(7, "usage policy output"), (b"{}", "usage policy output")],
)
def test_non_text_policy_output_is_refused_under_its_own_label(output: object, label: str) -> None:
    with pytest.raises(GridPolicyError) as caught:
        evaluate_policy(
            moment=_NIGHT,
            usage_policy_output=output,  # type: ignore[arg-type]
            home_quota_output=None,
            walltime_seconds=1800,
            cores=1,
        )

    assert str(caught.value) == f"{label} must be text or null"


def test_non_text_home_quota_output_is_refused_under_its_own_label() -> None:
    with pytest.raises(GridPolicyError) as caught:
        evaluate_policy(
            moment=_NIGHT,
            usage_policy_output=None,
            home_quota_output=7,  # type: ignore[arg-type]
            walltime_seconds=1800,
            cores=1,
        )

    assert str(caught.value) == "home quota output must be text or null"


@pytest.mark.parametrize("count", [-1, True, 1.0, "0"])
def test_an_invalid_account_job_count_is_refused_with_its_exact_reason(count: object) -> None:
    with pytest.raises(GridPolicyError) as caught:
        evaluate_policy(
            moment=_NIGHT,
            usage_policy_output=None,
            home_quota_output=None,
            walltime_seconds=1800,
            cores=1,
            account_job_count=count,  # type: ignore[arg-type]
        )

    assert str(caught.value) == (
        "account-wide active job count must be a non-negative integer or null"
    )


def test_usage_policy_output_that_is_not_an_object_is_reported_exactly() -> None:
    count, notes = parse_usage_policy_json("[]")

    assert count is None
    assert notes == ("usage policy output is not a JSON object",)


def test_usage_policy_output_missing_observed_keys_names_them_comma_separated() -> None:
    count, notes = parse_usage_policy_json('{"start_time": 1, "stop_time": 2}')

    assert count is None
    assert notes == ("usage policy output lacks observed keys: jobs, limits, total_jobs",)


def test_a_total_that_disagrees_with_the_job_list_is_refused_exactly() -> None:
    count, notes = parse_usage_policy_json(
        '{"start_time": 1, "stop_time": 2, "jobs": [], "total_jobs": 3, "limits": {}}'
    )

    assert count is None
    assert notes == ("usage policy total_jobs does not match jobs",)


def test_only_nfs_home_rows_are_read_and_the_worst_one_decides() -> None:
    """A non-home filesystem must not mask or cause a home quota verdict."""
    text = (
        "Disk quotas for user nflandre (uid 22441): \n"
        "     Filesystem  blocks   quota   limit   grace   files   quota   limit   grace\n"
        "nfs:/export/scratch 999  10  20       0   1   0   10       0\n"
        "nfs:/export/home 5  10  20       0   1   0   10       0"
    )

    exceeded, notes = parse_home_quota(text)

    assert exceeded is False
    assert notes == ()


def test_a_home_row_at_its_hard_limit_is_reported_with_that_filesystem() -> None:
    text = (
        "Disk quotas for user nflandre (uid 22441): \n"
        "     Filesystem  blocks   quota   limit   grace   files   quota   limit   grace\n"
        "nfs:/export/home 20  10  20       0   1   0   10       0"
    )

    exceeded, notes = parse_home_quota(text)

    assert exceeded is True
    assert notes == ("home quota hard limit reached on nfs:/export/home",)


def test_a_home_row_at_its_soft_limit_is_reported_as_a_soft_limit() -> None:
    text = (
        "Disk quotas for user nflandre (uid 22441): \n"
        "     Filesystem  blocks   quota   limit   grace   files   quota   limit   grace\n"
        "nfs:/export/home 10  10  20       0   1   0   10       0"
    )

    exceeded, notes = parse_home_quota(text)

    assert exceeded is True
    assert notes == ("home quota soft limit reached",)


def test_home_quota_output_that_cannot_be_parsed_is_unknown_not_clear() -> None:
    exceeded, notes = parse_home_quota("nfs:/export/home not a quota row")

    assert exceeded is None
    assert notes == ("home quota output could not be parsed",)


def test_a_device_name_containing_home_is_not_a_home_filesystem() -> None:
    """Only the mount point after the last colon decides; the device never does."""
    text = (
        "Disk quotas for user nflandre (uid 22441): \n"
        "     Filesystem  blocks   quota   limit   grace   files   quota   limit   grace\n"
        "home/nancy:/export/scratch 20  10  20       0   1   0   10       0\n"
        "nfs:/export/home 5  10  20       0   1   0   10       0"
    )

    assert parse_home_quota(text) == (False, ())


def test_only_the_last_colon_separates_the_device_from_the_mount_point() -> None:
    """A device may itself contain a colon; splitting from the left misreads it."""
    text = (
        "Disk quotas for user nflandre (uid 22441): \n"
        "     Filesystem  blocks   quota   limit   grace   files   quota   limit   grace\n"
        "nfs:home/nancy:/export/scratch 20  10  20       0   1   0   10       0\n"
        "nfs:/export/home 5  10  20       0   1   0   10       0"
    )

    assert parse_home_quota(text) == (False, ())


def test_the_file_count_hard_limit_is_read_as_a_hard_limit() -> None:
    """Blocks may be well inside quota while the inode hard limit is already spent."""
    text = (
        "Disk quotas for user nflandre (uid 22441): \n"
        "     Filesystem  blocks   quota   limit   grace   files   quota   limit   grace\n"
        "nfs:/export/home 5  10  20       0   10   0   10       0"
    )

    exceeded, notes = parse_home_quota(text)

    assert exceeded is True
    assert notes == ("home quota hard limit reached on nfs:/export/home",)


def test_the_first_of_two_equally_bad_home_filesystems_is_the_one_reported() -> None:
    """Reporting a different row than the worst-first one misdirects the operator."""
    text = (
        "Disk quotas for user nflandre (uid 22441): \n"
        "     Filesystem  blocks   quota   limit   grace   files   quota   limit   grace\n"
        "afs:/export/home 20  10  20       0   1   0   10       0\n"
        "zfs:/export/home 20  10  20       0   1   0   10       0"
    )

    exceeded, notes = parse_home_quota(text)

    assert exceeded is True
    assert notes == ("home quota hard limit reached on afs:/export/home",)


def test_a_later_hard_limit_outranks_an_earlier_soft_limit() -> None:
    """The worst state decides, not the first row that happens to be exceeded."""
    text = (
        "Disk quotas for user nflandre (uid 22441): \n"
        "     Filesystem  blocks   quota   limit   grace   files   quota   limit   grace\n"
        "zfs:/export/home 10  10  20       0   1   0   10       0\n"
        "afs:/export/home 20  10  20       0   1   0   10       0"
    )

    exceeded, notes = parse_home_quota(text)

    assert exceeded is True
    assert notes == ("home quota hard limit reached on afs:/export/home",)


def test_a_walltime_ending_one_microsecond_past_the_boundary_crosses_it() -> None:
    """The window's last instant is inclusive, so one microsecond decides the verdict."""
    start = datetime(2026, 9, 15, 18, 30, 0, 1, tzinfo=_PARIS)

    verdict = evaluate_policy(
        moment=start,
        usage_policy_output=_USAGE_JSON,
        home_quota_output=_QUOTA_CLEAR,
        walltime_seconds=1800,
        cores=1,
        allow_daytime=True,
        account_job_count=0,
    )

    assert verdict.decision is PolicyDecision.BLOCKED
    assert "job walltime crosses a Europe/Paris day/night boundary" in verdict.reasons


@pytest.mark.parametrize(
    ("payload", "notes"),
    [
        (
            {"start_time": 1, "stop_time": 2, "jobs": {}, "total_jobs": 0, "limits": {}},
            ("usage policy jobs must be a list",),
        ),
        (
            {
                "start_time": "2026-09-15 09:00:00 +0200",
                "stop_time": 2,
                "jobs": {},
                "total_jobs": 0,
                "limits": {},
            },
            (
                "usage policy start_time must be an integer",
                "usage policy jobs must be a list",
            ),
        ),
        (
            {
                "start_time": "not a timestamp",
                "stop_time": "2026-09-15 09:00:00 +0200",
                "jobs": {},
                "total_jobs": 0,
                "limits": {},
            },
            (
                "usage policy start_time must be an integer",
                "usage policy stop_time must be an integer",
                "usage policy jobs must be a list",
            ),
        ),
    ],
)
def test_a_usage_policy_payload_is_read_under_one_shape_not_a_mixture(
    payload: dict[str, object], notes: tuple[str, ...]
) -> None:
    """Both timestamps must be current-shaped before the current reader is used."""
    import json

    assert parse_usage_policy_json(json.dumps(payload))[1] == notes
