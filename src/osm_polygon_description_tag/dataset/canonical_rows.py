"""Shared deterministic canonical-row selection policy."""

from __future__ import annotations

import hashlib
import json
import math
from collections.abc import Mapping, Sequence
from datetime import UTC, datetime

import pyarrow as pa
from shapely import from_wkb, to_wkb

from osm_polygon_description_tag.dataset.schema import KEY_VALUE_COLUMNS, SCHEMA, mapping_to_pairs
from osm_polygon_description_tag.dataset.text import (
    description_row_has_successful_text,
    sql_literal,
    successful_description_text_sql,
)

CANONICAL_ROW_POLICY_VERSION = 5
_POLICY_TEXT = (
    "key=(osm_type,osm_id);text_filter=before_rank;"
    "winner=max(version NULLS LAST);then=max(timestamp NULLS LAST);"
    "timestamp=ISO8601_UTC;naive_timestamp=UTC;blank_timestamp=NULL;invalid_timestamp=reject;"
    "then=min(source_pbf);then=min(payload_sha256_json_fingerprint_canonical_wkb)"
)
CANONICAL_ROW_POLICY_SHA256 = hashlib.sha256(_POLICY_TEXT.encode("utf-8")).hexdigest()

CANONICAL_RANK_COLUMNS = (
    "source_pbf",
    "osm_type",
    "osm_id",
    "version",
    "timestamp",
)
CANONICAL_FINGERPRINT_COLUMNS = tuple(
    column for column in SCHEMA.names if column not in CANONICAL_RANK_COLUMNS
)


def _version(value: object) -> int:
    return int(value) if isinstance(value, int | float) else -1


def _parse_timestamp(value: object) -> datetime | None:
    if isinstance(value, datetime):
        return value
    if not isinstance(value, str) or not value.strip():
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None


def _is_missing_timestamp(value: object) -> bool:
    return value is None or (isinstance(value, str) and not value.strip())


def _timestamp_rank(value: object) -> float:
    parsed = _parse_timestamp(value)
    if parsed is None:
        if _is_missing_timestamp(value):
            return -math.inf
        raise ValueError("timestamp must be null or a valid ISO-8601 value")
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    return parsed.astimezone(UTC).timestamp()


def canonical_geometry_wkb(value: bytes | bytearray | memoryview) -> bytes:
    """Return the shared deterministic WKB representation of a geometry."""
    geometry = from_wkb(bytes(value))
    return to_wkb(geometry, byte_order=1, include_srid=False, output_dimension=2)


def canonical_geometry_wkb_sql(
    expression: str,
    *,
    input_is_geometry: bool = False,
) -> str:
    """Return SQL that converts a WKB or DuckDB geometry to canonical WKB."""
    wkb_expression = f"ST_AsWKB({expression})" if input_is_geometry else expression
    return f"ST_AsWKB(ST_GeomFromWKB({wkb_expression}))"


def _fingerprint_value(column: str, value: object) -> object:
    if value is None:
        return None
    if column in KEY_VALUE_COLUMNS:
        return mapping_to_pairs(value)
    if column == "geometry" and isinstance(value, bytes | bytearray | memoryview):
        return canonical_geometry_wkb(value).hex()
    return value


def _row_fingerprint(row: Mapping[str, object]) -> str:
    payload = json.dumps(
        {key: _fingerprint_value(key, row.get(key)) for key in CANONICAL_FINGERPRINT_COLUMNS},
        ensure_ascii=False,
        sort_keys=True,
        default=str,
        separators=(",", ":"),
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _rank_rows(
    rows: Sequence[Mapping[str, object]],
) -> tuple[tuple[Mapping[str, object], float], ...]:
    return tuple((row, _timestamp_rank(row.get("timestamp"))) for row in rows)


def _eligible_ranked_rows(
    rows: Sequence[tuple[Mapping[str, object], float]],
    *,
    require_successful_text: bool,
) -> tuple[tuple[Mapping[str, object], float], ...]:
    return tuple(
        (row, timestamp_rank)
        for row, timestamp_rank in rows
        if not require_successful_text or description_row_has_successful_text(row)
    )


def _canonical_row_order(
    candidate: tuple[Mapping[str, object], float],
) -> tuple[bool, int, float, str, str]:
    row, timestamp_rank = candidate
    return (
        row.get("version") is None,
        -_version(row.get("version")),
        -timestamp_rank,
        str(row.get("source_pbf", "")),
        _row_fingerprint(row),
    )


def _best_ranked_row(
    candidates: Sequence[tuple[Mapping[str, object], float]],
) -> Mapping[str, object]:
    if not candidates:
        raise ValueError("cannot select a canonical row from an empty group")
    return min(candidates, key=_canonical_row_order)[0]


def select_canonical_row(
    rows: Sequence[Mapping[str, object]],
    *,
    require_successful_text: bool = False,
) -> Mapping[str, object]:
    """Select the stable canonical row for one OSM identity group.

    Timestamps accept datetimes or ISO-8601 strings. Naive values are UTC;
    null and blank values rank last; invalid nonblank values raise ``ValueError``.
    Text-aware selection filters rows before ranking. Remaining ties sort by
    source name and then the canonical payload fingerprint.
    """
    ranked_rows = _rank_rows(rows)
    candidates = _eligible_ranked_rows(
        ranked_rows,
        require_successful_text=require_successful_text,
    )
    return _best_ranked_row(candidates)


# ``json.dumps(ensure_ascii=False)`` escapes exactly these characters. The
# backslash comes first so that the later replacements are not escaped again.
_JSON_STRING_ESCAPES = tuple(
    (character, json.dumps(character, ensure_ascii=False)[1:-1])
    for character in ("\\", '"', *map(chr, range(0x20)))
    if json.dumps(character, ensure_ascii=False)[1:-1] != character
)
_JSON_STRING_ESCAPE_PATTERN = r'[\x00-\x1f"\\]'


def _json_string_sql(expression: str) -> str:
    """Return SQL that writes a non-null string exactly as ``json.dumps`` does.

    DuckDB's own JSON escaping uses uppercase hex for control characters, so
    the payload is built here. Strings without an escapable character skip the
    replacement chain.
    """
    escaped = expression
    for character, replacement in _JSON_STRING_ESCAPES:
        escaped = f"replace({escaped}, chr({ord(character)}), {sql_literal(replacement)})"
    return (
        f"'\"' || CASE WHEN regexp_matches({expression}, "
        f"{sql_literal(_JSON_STRING_ESCAPE_PATTERN)}) THEN {escaped} ELSE {expression} END || '\"'"
    )


def _json_double_sql(expression: str) -> str:
    """Return SQL that writes a non-null double exactly as ``json.dumps`` does.

    ``CAST(... AS VARCHAR)`` yields the shortest round-trip text that Python's
    ``repr`` also yields for finite values, such as ``1e-05`` and ``1e+16``.
    """
    return (
        f"CASE WHEN isnan({expression}) THEN 'NaN' "
        f"WHEN isinf({expression}) AND {expression} > 0 THEN 'Infinity' "
        f"WHEN isinf({expression}) THEN '-Infinity' "
        f"ELSE CAST({expression} AS VARCHAR) END"
    )


def _concat_sql(*parts: str) -> str:
    return " || ".join(parts)


def _key_value_json_sql(entries: str) -> str:
    """Return SQL for a key/value list in the Python ``mapping_to_pairs`` order."""
    pair = _concat_sql(
        sql_literal('{"key":'),
        _json_string_sql("e.key"),
        sql_literal(',"value":'),
        _json_string_sql("e.value"),
        sql_literal("}"),
    )
    elements = f"array_to_string(list_transform(list_sort({entries}), e -> {pair}), ',')"
    return _concat_sql(sql_literal("["), elements, sql_literal("]"))


def _fingerprint_value_sql(column: str, *, key_value_columns_are_maps: bool) -> str:
    """Return SQL that writes one fingerprint member as ``_fingerprint_value`` does."""
    quoted = f'"{column}"'
    if column == "geometry":
        encoded = f"'\"' || lower(hex({canonical_geometry_wkb_sql(quoted)})) || '\"'"
    elif column in KEY_VALUE_COLUMNS:
        entries = f"map_entries({quoted})" if key_value_columns_are_maps else quoted
        encoded = _key_value_json_sql(entries)
    else:
        field_type = SCHEMA.field(column).type
        if pa.types.is_floating(field_type):
            encoded = _json_double_sql(f"CAST({quoted} AS DOUBLE)")
        elif pa.types.is_integer(field_type):
            encoded = f"CAST(CAST({quoted} AS BIGINT) AS VARCHAR)"
        elif pa.types.is_string(field_type):
            encoded = _json_string_sql(quoted)
        else:
            raise ValueError(f"no canonical fingerprint encoding for column {column!r}")
    return f"CASE WHEN {quoted} IS NULL THEN 'null' ELSE {encoded} END"


def _full_row_fingerprint_sql(*, key_value_columns_are_maps: bool = False) -> str:
    """Return SQL for the SHA-256 of the exact payload that ``_row_fingerprint`` hashes."""
    members = [
        _concat_sql(
            sql_literal(f'"{column}":'),
            _fingerprint_value_sql(column, key_value_columns_are_maps=key_value_columns_are_maps),
        )
        for column in sorted(CANONICAL_FINGERPRINT_COLUMNS)
    ]
    # Every member is non-NULL, so one variadic concat builds the payload in a
    # single pass instead of copying the growing string once per member.
    pieces = [sql_literal("{")]
    for position, member in enumerate(members):
        if position:
            pieces.append(sql_literal(","))
        pieces.append(member)
    pieces.append(sql_literal("}"))
    return f"sha256(concat({', '.join(pieces)}))"


def canonical_row_order_sql(*, key_value_columns_are_maps: bool = False) -> str:
    """Return the SQL order expression implementing the canonical policy."""
    return (
        "version DESC NULLS LAST, "
        "timestamp DESC NULLS LAST, "
        "source_pbf ASC, "
        f"{_full_row_fingerprint_sql(key_value_columns_are_maps=key_value_columns_are_maps)} ASC"
    )


def canonical_rows_sql(
    relation: str,
    columns: Sequence[str],
    *,
    key_value_columns_are_maps: bool = False,
    require_successful_text: bool = False,
) -> str:
    """Return one canonical row per identity from a relation.

    When ``require_successful_text`` is true, text eligibility is applied
    before ranking so an invalid higher-ranked duplicate cannot hide a valid
    lower-ranked row for the same OSM identity.
    """
    selected = tuple(dict.fromkeys(columns))
    if not selected:
        raise ValueError("unique-row views require at least one selected column")
    unknown = set(selected) - set(SCHEMA.names)
    if unknown:
        raise ValueError(f"unsupported unique-row columns: {sorted(unknown)}")
    selected_sql = ", ".join(selected)
    canonical_order = canonical_row_order_sql(key_value_columns_are_maps=key_value_columns_are_maps)
    text_filter = ""
    if require_successful_text:
        text_filter = "WHERE " + successful_description_text_sql(
            localized_is_map=key_value_columns_are_maps,
        )
    return f"""
        SELECT {selected_sql}
        FROM (
            SELECT *, ROW_NUMBER() OVER (
                PARTITION BY osm_type, osm_id
                ORDER BY {canonical_order}
            ) AS _canonical_rank
            FROM {relation}
            {text_filter}
        ) ranked
        WHERE _canonical_rank = 1
    """  # noqa: S608 - relation/columns are internal allowlisted SQL fragments


TEXT_CANONICAL_COLUMN = "_text_canonical"
# Raw Parquet stores localized descriptions as a list of entries, not a map.
_RAW_TEXT_OK_SQL = successful_description_text_sql(localized_is_map=False)


def canonical_rows_with_text_flag_sql(relation: str, columns: Sequence[str]) -> str:
    """Return both canonical views of a relation in one ranked pass.

    Yields every row that is canonical under plain ranking or under
    text-aware ranking (text filtered before ranking), plus a boolean
    ``_text_canonical`` column that is true exactly for the rows
    :func:`canonical_rows_sql` returns with ``require_successful_text``.
    """
    selected = tuple(dict.fromkeys(columns))
    if not selected:
        raise ValueError("unique-row views require at least one selected column")
    unknown = set(selected) - set(SCHEMA.names)
    if unknown:
        raise ValueError(f"unsupported unique-row columns: {sorted(unknown)}")
    order = canonical_row_order_sql()
    text_ok = _RAW_TEXT_OK_SQL
    return f"""
        SELECT {", ".join(selected)},
            (_text_ok AND _text_rank = 1) AS {TEXT_CANONICAL_COLUMN}
        FROM (
            SELECT *,
                ROW_NUMBER() OVER (PARTITION BY osm_type, osm_id ORDER BY {order}) AS _rank,
                ROW_NUMBER() OVER (
                    PARTITION BY osm_type, osm_id, _text_ok ORDER BY {order}
                ) AS _text_rank
            FROM (SELECT *, COALESCE({text_ok}, FALSE) AS _text_ok FROM {relation})
        ) ranked
        WHERE _rank = 1 OR (_text_ok AND _text_rank = 1)
    """  # noqa: S608 - relation/columns are internal allowlisted SQL fragments


__all__ = [
    "CANONICAL_FINGERPRINT_COLUMNS",
    "CANONICAL_RANK_COLUMNS",
    "CANONICAL_ROW_POLICY_SHA256",
    "CANONICAL_ROW_POLICY_VERSION",
    "TEXT_CANONICAL_COLUMN",
    "canonical_geometry_wkb",
    "canonical_geometry_wkb_sql",
    "canonical_row_order_sql",
    "canonical_rows_sql",
    "canonical_rows_with_text_flag_sql",
    "select_canonical_row",
]
