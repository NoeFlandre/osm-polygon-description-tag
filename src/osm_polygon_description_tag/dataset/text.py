"""Shared text predicates for extracted description artifacts.

The extraction and reporting layers use the same boundary: a counted value is
text, has no leading or trailing whitespace, and is not empty after trimming.
The SQL expression in this module mirrors the Python predicate so a row is not
included merely because one consumer happens to see a non-blank value.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Any, TypeGuard

TEXT_CONTRACT_VERSION = 1

# These are the code points removed by Python's ``str.strip`` whitespace
# predicate. DuckDB's one-argument ``trim`` only removes ordinary spaces, so
# the SQL mirror must pass the same complete character set explicitly.
_PYTHON_STRIP_CHARACTERS = "".join(
    chr(codepoint)
    for codepoint in (
        9,
        10,
        11,
        12,
        13,
        28,
        29,
        30,
        31,
        32,
        133,
        160,
        5760,
        *range(8192, 8203),
        8232,
        8233,
        8239,
        8287,
        12288,
    )
)


def sql_literal(value: str) -> str:
    """Return ``value`` quoted as a SQL string literal."""
    return "'" + value.replace("'", "''") + "'"


def _trim_sql(column: str) -> str:
    return f"trim({column}, {sql_literal(_PYTHON_STRIP_CHARACTERS)})"


def is_nonempty_text(value: object) -> TypeGuard[str]:
    """Return whether a value is a string with non-whitespace content."""
    return isinstance(value, str) and bool(value.strip())


def trimmed_nonempty_text(value: object) -> str | None:
    """Return a canonical trimmed text value, or ``None`` when unusable."""
    if not isinstance(value, str):
        return None
    trimmed = value.strip()
    return trimmed or None


def is_trimmed_nonempty_text(value: object) -> TypeGuard[str]:
    """Return whether ``value`` is already the canonical counted text form."""
    return isinstance(value, str) and bool(value) and value == value.strip()


def _localized_sequence_values(value: Sequence[object]) -> tuple[object, ...] | None:
    values: list[object] = []
    for entry in value:
        if not isinstance(entry, Mapping):
            return None
        values.append(entry.get("value"))
    return tuple(values)


def _localized_values(value: object) -> tuple[object, ...] | None:
    if value is None:
        return ()
    if isinstance(value, Mapping):
        return tuple(value.values())
    if isinstance(value, Sequence) and not isinstance(value, str | bytes):
        return _localized_sequence_values(value)
    return None


def _valid_localized_values(value: object) -> tuple[object, ...] | None:
    values = _localized_values(value)
    if values is None or any(not is_trimmed_nonempty_text(item) for item in values):
        return None
    return values


def has_successful_description_text(
    description: object,
    localized_descriptions: object = None,
) -> bool:
    """Return whether a final row contains only valid, canonical text values.

    A malformed localized container or value makes the row ineligible. This
    deliberately treats the row as the unit of the final-artifact contract:
    callers cannot count a valid sibling value while silently retaining a
    malformed or untrimmed description value.
    """
    if description is not None and not is_trimmed_nonempty_text(description):
        return False
    localized_values = _valid_localized_values(localized_descriptions)
    if localized_values is None:
        return False
    return bool(description) or bool(localized_values)


def successful_description_text_sql(
    *,
    description_column: str = "description",
    localized_column: str = "localized_descriptions",
    localized_is_map: bool,
) -> str:
    """Return the DuckDB predicate matching the final text contract."""
    entries = f"map_entries({localized_column})" if localized_is_map else localized_column
    base = (
        f"{description_column} IS NOT NULL AND "
        f"length({description_column}) > 0 AND "
        f"{description_column} = {_trim_sql(description_column)}"
    )
    localized_valid = (
        f"EXISTS (SELECT 1 FROM unnest({entries}) AS localized(entry) "  # noqa: S608 - expressions use fixed internal column names
        "WHERE entry.value IS NOT NULL "
        "AND length(entry.value) > 0 "
        f"AND entry.value = {_trim_sql('entry.value')})"
    )
    # pragma: no mutate start - SQL casing is semantically equivalent in DuckDB
    localized_invalid = (
        f"EXISTS (SELECT 1 FROM unnest({entries}) AS localized(entry) "  # noqa: S608 - expressions use fixed internal column names
        "WHERE entry.value IS NULL "
        "OR length(entry.value) = 0 "
        f"OR entry.value <> {_trim_sql('entry.value')})"
    )
    # pragma: no mutate end
    return (
        f"({base} OR {localized_valid}) "
        f"AND ({description_column} IS NULL OR {base}) "
        f"AND NOT ({localized_invalid})"
    )


def description_row_has_successful_text(row: Mapping[str, Any]) -> bool:
    """Return the final-text predicate for a row-like mapping."""
    return has_successful_description_text(
        row.get("description"), row.get("localized_descriptions")
    )


__all__ = [
    "TEXT_CONTRACT_VERSION",
    "description_row_has_successful_text",
    "has_successful_description_text",
    "is_nonempty_text",
    "is_trimmed_nonempty_text",
    "sql_literal",
    "successful_description_text_sql",
    "trimmed_nonempty_text",
]
