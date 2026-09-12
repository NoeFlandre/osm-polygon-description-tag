"""Exact refusals of the worker's bounded-resource arguments.

The batch size, the processing budget, and the repeated-text cache are the
three knobs that keep one Grid'5000 job inside its walltime and its memory.
Each refusal names the knob it refused, so each message is asserted whole.
"""

import math

import pytest

from osm_polygon_description_tag.dataset.languages.worker import (
    MAX_BATCH_SIZE,
    BoundedTextCache,
    ProcessingBudget,
)


@pytest.mark.parametrize(
    ("seconds", "error", "message"),
    [
        ("60", TypeError, "budget seconds must be a real number"),
        (True, TypeError, "budget seconds must be a real number"),
        (None, TypeError, "budget seconds must be a real number"),
        (math.inf, ValueError, "budget seconds must be finite"),
        (math.nan, ValueError, "budget seconds must be finite"),
        (0, ValueError, "budget seconds must be positive"),
        (-1.0, ValueError, "budget seconds must be positive"),
    ],
)
def test_a_rejected_processing_budget_states_which_rule_it_broke(
    seconds: object, error: type[Exception], message: str
) -> None:
    with pytest.raises(error) as caught:
        ProcessingBudget(seconds)  # type: ignore[arg-type]

    assert str(caught.value) == message


def test_the_smallest_positive_budget_is_accepted_and_is_spent_by_elapsed_time() -> None:
    """Positivity is the rule, so any positive budget must be kept and honoured."""
    ticks = iter([0.0, 0.5, 1.0])
    budget = ProcessingBudget(1.0, clock=lambda: next(ticks))
    budget.start()

    assert not budget.exhausted()
    assert budget.exhausted()


@pytest.mark.parametrize("max_entries", [0, -1, True, 1.0, "8", None])
def test_a_rejected_cache_size_states_which_rule_it_broke(max_entries: object) -> None:
    with pytest.raises(ValueError) as caught:
        BoundedTextCache(max_entries)  # type: ignore[arg-type]

    assert str(caught.value) == "cache max_entries must be a positive integer"


def test_a_single_entry_cache_is_accepted_and_stays_bounded() -> None:
    """One entry is the documented minimum, and eviction must keep it at one."""
    cache = BoundedTextCache(1)
    calls: list[str] = []

    def analyse(text: str) -> object:
        calls.append(text)
        return text

    cache.analysis_for("first", analyse)  # type: ignore[arg-type]
    cache.analysis_for("second", analyse)  # type: ignore[arg-type]
    cache.analysis_for("first", analyse)  # type: ignore[arg-type]

    assert len(cache) == 1
    assert calls == ["first", "second", "first"]


def test_a_repeated_text_is_detected_once_within_the_bound() -> None:
    cache = BoundedTextCache(4)
    calls: list[str] = []

    def analyse(text: str) -> object:
        calls.append(text)
        return text

    cache.analysis_for("same", analyse)  # type: ignore[arg-type]
    cache.analysis_for("same", analyse)  # type: ignore[arg-type]

    assert calls == ["same"]
    assert len(cache) == 1


def test_the_documented_maximum_batch_size_is_a_positive_bound() -> None:
    assert type(MAX_BATCH_SIZE) is int
    assert MAX_BATCH_SIZE > 0
