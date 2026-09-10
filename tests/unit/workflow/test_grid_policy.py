"""Fail-closed Grid'5000 usage-policy and quota preflight."""

import json
from datetime import UTC, datetime, timedelta
from zoneinfo import ZoneInfo

import pytest

from osm_polygon_description_tag.workflow.grid_policy import (
    GRID5000_TIMEZONE,
    MAX_EVIDENCE_AGE_SECONDS,
    MAX_WALLTIME_SECONDS,
    GridPolicyError,
    PolicyDecision,
    evaluate_policy,
    is_weekday_daytime,
    parse_home_quota,
    parse_usage_policy_json,
    policy_evidence_is_fresh,
)

OBSERVED_POLICY = json.dumps(
    {"start_time": 0, "stop_time": 0, "jobs": [], "total_jobs": 0, "limits": {}}
)
CLEAR_QUOTA = (
    "Filesystem blocks quota limit grace files quota limit grace\n"
    "/home/user 100 1000 2000 0 10 1000 2000 0\n"
)


@pytest.mark.parametrize(
    ("row", "expected"),
    [
        ("nfs.example:/export/home 100 1000 2000 0 10 1000 2000 0", False),
        ("/home/user 100 1000 2000 0 2000* 1000 2000 0", True),
        ("/home/user 100 1000 2000 0 1001* 1000 2000 123456", True),
        ("/home/user 100 1000 2000 0 1000 1000 2000 0", True),
        ("/home/user 100 0 0 0 10 0 0 0", False),
        ("/home/user 2000* 1000 2000 0 10 1000 2000 0", True),
        ("/home/user 100 1000 2000", None),
        ("/home/user 100 1000 2000 0 broken 1000 2000 0", None),
        ("nfs.example:/export/scratch 100 1000 2000 0 10 1000 2000 0", None),
    ],
)
def test_real_raw_grace_quota_checks_blocks_and_inodes(row: str, expected: bool | None) -> None:
    assert parse_home_quota(row)[0] is expected


def test_a_malformed_home_row_cannot_hide_behind_a_clear_one() -> None:
    assert parse_home_quota(CLEAR_QUOTA + "/home/other missing quota fields\n")[0] is None


@pytest.mark.parametrize("cores", [True, 1.0, "1"])
def test_core_count_must_be_an_actual_integer(cores: object) -> None:
    assert _evaluate(cores=cores).decision is PolicyDecision.BLOCKED


def _paris(text: str) -> datetime:
    return datetime.fromisoformat(text).replace(tzinfo=GRID5000_TIMEZONE)


def _evaluate(**overrides: object) -> object:
    arguments: dict[str, object] = {
        "moment": _paris("2026-09-05T22:00:00"),
        "usage_policy_output": OBSERVED_POLICY,
        "home_quota_output": CLEAR_QUOTA,
        "account_job_count": 0,
        "walltime_seconds": MAX_WALLTIME_SECONDS,
        "cores": 1,
    }
    return evaluate_policy(**{**arguments, **overrides})  # type: ignore[arg-type]


@pytest.mark.parametrize(
    ("moment", "expected"),
    [
        ("2026-09-07T09:00:00", True),
        ("2026-09-07T18:59:59", True),
        ("2026-09-07T08:59:59", False),
        ("2026-09-07T19:00:00", False),
        ("2026-09-11T12:00:00", True),
        ("2026-09-05T12:00:00", False),
        ("2026-09-06T12:00:00", False),
    ],
)
def test_weekday_daytime_uses_europe_paris(moment: str, expected: bool) -> None:
    assert is_weekday_daytime(_paris(moment)) is expected


def test_daytime_is_evaluated_in_paris_not_in_the_input_zone() -> None:
    # Monday 08:30 UTC is 10:30 in Paris during CEST: inside the daytime window.
    assert is_weekday_daytime(datetime(2026, 9, 7, 8, 30, tzinfo=UTC)) is True
    # Tuesday 03:30 in Tokyo is Monday 20:30 in Paris: outside it.
    assert is_weekday_daytime(datetime(2026, 9, 8, 3, 30, tzinfo=ZoneInfo("Asia/Tokyo"))) is False


def test_a_naive_time_is_rejected() -> None:
    with pytest.raises(GridPolicyError, match="timezone aware"):
        is_weekday_daytime(datetime(2026, 9, 7, 12, 0))


def test_the_observed_usage_policy_schema_yields_a_job_count() -> None:
    assert parse_usage_policy_json(OBSERVED_POLICY) == (0, ())


def test_the_current_usagepolicycheck_json_schema_is_accepted() -> None:
    live_policy = json.dumps(
        {
            "start_time": "2026-09-02 10:48:36 +0200",
            "stop_time": "2026-09-16 10:48:36 +0200",
            "jobs": {},
            "total_jobs": {},
            "limits": {"nancy": {"gros": {"time": 15940800, "nb_cores": 2214}}},
        }
    )
    assert parse_usage_policy_json(live_policy) == (0, ())


@pytest.mark.parametrize(
    ("field", "message"),
    [
        ("jobs", "usage policy jobs must be an object"),
        ("total_jobs", "usage policy total_jobs must be an object"),
        ("limits", "usage policy limits must be an object"),
    ],
)
def test_the_current_usage_policy_schema_rejects_a_non_object_field(
    field: str, message: str
) -> None:
    """The live OAR3 shape reports every field it cannot positively interpret."""
    payload = {
        "start_time": "2026-09-02 10:48:36 +0200",
        "stop_time": "2026-09-16 10:48:36 +0200",
        "jobs": {},
        "total_jobs": {},
        "limits": {},
    }
    payload[field] = []

    count, notes = parse_usage_policy_json(json.dumps(payload))

    assert count is None
    assert message in notes


def test_a_negative_legacy_total_is_refused_without_inventing_a_count() -> None:
    """A legacy payload whose total is negative stays unknown, never zero."""
    payload = json.dumps(
        {"start_time": 0, "stop_time": 0, "jobs": [], "total_jobs": -1, "limits": {}}
    )

    count, notes = parse_usage_policy_json(payload)

    assert count is None
    assert "usage policy total_jobs is not a non-negative integer" in notes


def test_usage_policy_fields_have_strict_types_and_consistent_job_count() -> None:
    malformed = json.dumps(
        {"start_time": "0", "stop_time": 0, "jobs": [], "total_jobs": 0, "limits": {}}
    )
    assert parse_usage_policy_json(malformed)[0] is None

    inconsistent = json.dumps(
        {"start_time": 0, "stop_time": 0, "jobs": [{"id": 1}], "total_jobs": 0, "limits": {}}
    )
    count, notes = parse_usage_policy_json(inconsistent)
    assert count is None
    assert any("does not match jobs" in note for note in notes)


def test_historical_usage_policy_total_is_not_treated_as_active_jobs() -> None:
    historical = json.dumps(
        {
            "start_time": 0,
            "stop_time": 0,
            "jobs": [{"id": index} for index in range(7)],
            "total_jobs": 7,
            "limits": {},
        }
    )

    verdict = _evaluate(usage_policy_output=historical, account_job_count=0)

    assert verdict.decision is PolicyDecision.ALLOWED
    assert verdict.evidence.active_job_count == 0


def test_account_wide_oarstat_active_job_blocks_even_when_history_is_empty() -> None:
    verdict = _evaluate(account_job_count=1)

    assert verdict.decision is PolicyDecision.BLOCKED
    assert verdict.evidence.active_job_count == 1
    assert any("concurrency is limited to 1" in reason for reason in verdict.reasons)


def test_missing_account_wide_oarstat_evidence_is_unknown() -> None:
    verdict = _evaluate(account_job_count=None)

    assert verdict.decision is PolicyDecision.UNKNOWN
    assert verdict.evidence.active_job_count is None
    assert any("account-wide" in reason for reason in verdict.reasons)


@pytest.mark.parametrize("value", [True, -1, 1.5, "0"])
def test_injected_account_job_count_is_strictly_typed(value: object) -> None:
    with pytest.raises(GridPolicyError, match="account-wide active job count"):
        _evaluate(account_job_count=value)


def test_quota_without_a_home_filesystem_is_unknown_not_clear() -> None:
    assert parse_home_quota("/scratch 100 1000 2000 0 10 1000 2000 0\n")[0] is None


def test_policy_evidence_must_be_recent_and_timezone_aware() -> None:
    moment = _paris("2026-09-05T22:00:00")
    assert policy_evidence_is_fresh(moment, moment)
    assert not policy_evidence_is_fresh(
        moment, moment - timedelta(seconds=MAX_EVIDENCE_AGE_SECONDS + 1)
    )
    with pytest.raises(GridPolicyError, match="timezone aware"):
        policy_evidence_is_fresh(moment, datetime(2026, 9, 5, 22, 0))


@pytest.mark.parametrize("age_limit", [0, -1, True, 1.0])
def test_evidence_age_limit_must_be_a_positive_integer(age_limit: object) -> None:
    moment = _paris("2026-09-05T22:00:00")
    with pytest.raises(GridPolicyError, match="^maximum evidence age must be a positive integer$"):
        policy_evidence_is_fresh(moment, moment, max_age_seconds=age_limit)  # type: ignore[arg-type]


def test_freshness_includes_the_exact_age_limit_but_rejects_future_captures() -> None:
    moment = _paris("2026-09-05T22:00:00")
    assert policy_evidence_is_fresh(moment, moment - timedelta(seconds=MAX_EVIDENCE_AGE_SECONDS))
    assert not policy_evidence_is_fresh(moment, moment + timedelta(microseconds=1))
    verdict = _evaluate(moment=moment, evidence_captured_at=moment, require_fresh_evidence=True)
    assert verdict.decision is PolicyDecision.ALLOWED
    assert verdict.evidence.captured_at == moment
    assert verdict.to_payload()["evidence"]["captured_at"] == moment.isoformat()


@pytest.mark.parametrize(
    ("field", "value", "message"),
    [
        ("jobs", {}, "usage policy jobs must be a list"),
        ("jobs", [1], "usage policy jobs must contain objects"),
        ("limits", [], "usage policy limits must be an object"),
        ("total_jobs", -1, "usage policy total_jobs is not a non-negative integer"),
        ("start_time", True, "usage policy start_time must be an integer"),
        ("stop_time", "0", "usage policy stop_time must be an integer"),
    ],
)
def test_usage_evidence_rejects_malformed_fields_with_actionable_notes(
    field: str, value: object, message: str
) -> None:
    payload = json.loads(OBSERVED_POLICY)
    payload[field] = value
    assert parse_usage_policy_json(json.dumps(payload)) == (None, (message,))


def test_non_text_usage_evidence_is_not_silently_coerced() -> None:
    assert parse_usage_policy_json(None) == (None, ("usage policy output must be text",))  # type: ignore[arg-type]


@pytest.mark.parametrize("field", ["usage_policy_output", "home_quota_output"])
def test_preflight_rejects_non_text_command_outputs(field: str) -> None:
    with pytest.raises(GridPolicyError, match="must be text or null$"):
        _evaluate(**{field: 7})


def test_strict_policy_evaluation_rejects_missing_or_stale_capture_time() -> None:
    moment = _paris("2026-09-05T22:00:00")
    missing = evaluate_policy(
        moment=moment,
        usage_policy_output=OBSERVED_POLICY,
        home_quota_output=CLEAR_QUOTA,
        walltime_seconds=MAX_WALLTIME_SECONDS,
        cores=1,
        require_fresh_evidence=True,
    )
    assert missing.decision is PolicyDecision.UNKNOWN
    assert any("freshness" in reason for reason in missing.reasons)

    stale = evaluate_policy(
        moment=moment,
        usage_policy_output=OBSERVED_POLICY,
        home_quota_output=CLEAR_QUOTA,
        walltime_seconds=MAX_WALLTIME_SECONDS,
        cores=1,
        evidence_captured_at=moment.replace(hour=20),
        require_fresh_evidence=True,
    )
    assert stale.decision is PolicyDecision.UNKNOWN
    assert any("freshness" in reason for reason in stale.reasons)


@pytest.mark.parametrize(
    ("text", "message"),
    [
        ("not json", "not JSON"),
        ("[]", "not a JSON object"),
        (json.dumps({"total_jobs": 0}), "lacks observed keys"),
        (
            json.dumps(
                {"start_time": 0, "stop_time": 0, "jobs": [], "total_jobs": "0", "limits": {}}
            ),
            "not a non-negative integer",
        ),
        (
            json.dumps(
                {"start_time": 0, "stop_time": 0, "jobs": [], "total_jobs": True, "limits": {}}
            ),
            "not a non-negative integer",
        ),
    ],
)
def test_an_unrecognised_usage_policy_shape_is_never_read_as_approval(
    text: str, message: str
) -> None:
    count, notes = parse_usage_policy_json(text)

    assert count is None
    assert any(message in note for note in notes)


def test_an_unparseable_quota_report_is_unknown_rather_than_clear() -> None:
    assert parse_home_quota("no quota information here") == (
        None,
        ("home quota output could not be parsed",),
    )


def test_quota_limits_are_detected() -> None:
    assert parse_home_quota(CLEAR_QUOTA) == (False, ())

    soft, soft_notes = parse_home_quota("/home/user 1500 1000 2000 0 10 1000 2000 0\n")
    assert soft is True
    assert soft_notes == ("home quota soft limit reached",)

    hard, hard_notes = parse_home_quota("/home/user 2500 1000 2000 0 10 1000 2000 0\n")
    assert hard is True
    assert any("hard limit" in note for note in hard_notes)


def test_an_offhours_request_with_clear_evidence_is_allowed() -> None:
    verdict = _evaluate()

    assert verdict.decision is PolicyDecision.ALLOWED
    assert verdict.may_submit
    assert verdict.evidence.active_job_count == 0
    assert verdict.evidence.home_quota_exceeded is False
    assert verdict.evidence.usage_policy_parsed


def test_weekday_daytime_is_blocked_by_default() -> None:
    verdict = _evaluate(moment=_paris("2026-09-07T12:00:00"))

    assert verdict.decision is PolicyDecision.BLOCKED
    assert not verdict.may_submit
    assert any("daytime accounting is not verifiable" in reason for reason in verdict.reasons)


def test_weekday_daytime_can_be_enabled_explicitly() -> None:
    verdict = _evaluate(moment=_paris("2026-09-07T12:00:00"), allow_daytime=True)

    assert verdict.decision is PolicyDecision.ALLOWED
    assert verdict.reasons == (
        "permitted window, one core, bounded walltime, no active job, quota available",
    )


@pytest.mark.parametrize(
    ("start", "allowed"),
    [
        ("2026-09-07T08:30:00", True),
        ("2026-09-07T08:30:01", False),
        ("2026-09-07T18:30:00", True),
        ("2026-09-07T18:30:01", False),
        ("2026-09-06T08:50:00", True),
        ("2026-09-06T23:50:00", True),
    ],
)
def test_walltime_must_fit_one_day_or_night_window(start: str, allowed: bool) -> None:
    verdict = _evaluate(moment=_paris(start), walltime_seconds=1800, allow_daytime=True)

    assert (verdict.decision is PolicyDecision.ALLOWED) is allowed
    if not allowed:
        assert "job walltime crosses a Europe/Paris day/night boundary" in verdict.reasons


def test_unconsulted_evidence_is_unknown_and_never_allowed() -> None:
    verdict = _evaluate(
        usage_policy_output=None,
        home_quota_output=None,
        account_job_count=None,
    )

    assert verdict.decision is PolicyDecision.UNKNOWN
    assert not verdict.may_submit
    assert "account-wide active job count is unknown" in verdict.reasons
    assert "home quota state is unknown" in verdict.reasons
    assert not verdict.evidence.usage_policy_parsed


def test_a_zero_exit_with_an_unreadable_body_is_unknown() -> None:
    verdict = _evaluate(usage_policy_output="")

    assert verdict.decision is PolicyDecision.UNKNOWN
    assert "usage policy evidence is invalid" in verdict.reasons


def test_an_active_job_blocks_a_second_submission() -> None:
    output = json.dumps(
        {"start_time": 0, "stop_time": 0, "jobs": [{"id": 1}], "total_jobs": 1, "limits": {}}
    )

    verdict = _evaluate(usage_policy_output=output, account_job_count=1)

    assert verdict.decision is PolicyDecision.BLOCKED
    assert any("concurrency is limited to 1" in reason for reason in verdict.reasons)


def test_an_exhausted_quota_blocks_submission() -> None:
    verdict = _evaluate(home_quota_output="/home/u 2500 1000 2000 0 10 1000 2000 0\n")

    assert verdict.decision is PolicyDecision.BLOCKED
    assert "home storage quota is exhausted" in verdict.reasons


@pytest.mark.parametrize(
    ("overrides", "message"),
    [
        ({"walltime_seconds": MAX_WALLTIME_SECONDS + 1}, "exceeds the 1800s limit"),
        ({"walltime_seconds": 0}, "positive number of seconds"),
        ({"cores": 2}, "exactly 1 core must be requested"),
        ({"cores": 0}, "exactly 1 core must be requested"),
    ],
)
def test_an_oversized_request_is_blocked(overrides: dict[str, object], message: str) -> None:
    verdict = _evaluate(**overrides)

    assert verdict.decision is PolicyDecision.BLOCKED
    assert any(message in reason for reason in verdict.reasons)


def test_blocking_reasons_take_precedence_over_unknown_ones() -> None:
    verdict = _evaluate(cores=4, usage_policy_output=None, home_quota_output=None)

    assert verdict.decision is PolicyDecision.BLOCKED


def test_the_verdict_payload_is_json_serializable() -> None:
    payload = _evaluate().to_payload()

    assert json.loads(json.dumps(payload))["may_submit"] is True
    assert payload["decision"] == "allowed"
    assert payload["evidence"]["active_job_count"] == 0
