"""Part and receipt file names: generated names are recognised, ordered and unique."""

from __future__ import annotations

from hypothesis import example, given
from hypothesis import strategies as st

from osm_polygon_description_tag.dataset.languages.checkpoint import (
    is_generated_part_name,
    is_generated_receipt_name,
    part_name_for_offset,
    receipt_name_for_part,
)

# Part filenames have a fixed 20-digit offset; overflow is rejected explicitly.
_OFFSETS = st.integers(min_value=0, max_value=10**20 - 1)


@given(_OFFSETS)
@example(0)
@example(10**12 - 1)
@example(10**20 - 1)
def test_a_generated_part_name_is_recognised_and_has_a_matching_receipt(offset: int) -> None:
    part = part_name_for_offset(offset)

    assert is_generated_part_name(part)
    assert not is_generated_receipt_name(part)
    assert is_generated_receipt_name(receipt_name_for_part(part))
    assert not is_generated_part_name(receipt_name_for_part(part))


@given(_OFFSETS, _OFFSETS)
def test_part_names_are_injective_and_sort_like_their_offsets(first: int, second: int) -> None:
    a, b = part_name_for_offset(first), part_name_for_offset(second)

    assert (a == b) == (first == second)
    assert (a < b) == (first < second)


@given(st.text(max_size=20))
def test_names_that_were_not_generated_are_not_recognised_as_both_kinds(name: str) -> None:
    assert not (is_generated_part_name(name) and is_generated_receipt_name(name))
