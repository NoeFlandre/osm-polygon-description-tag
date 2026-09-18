"""Shared deterministic canonical-row selection policy."""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping, Sequence
from datetime import UTC, datetime

from shapely import from_wkb, to_wkb

from osm_polygon_description_tag.dataset.schema import KEY_VALUE_COLUMNS, SCHEMA, mapping_to_pairs
from osm_polygon_description_tag.dataset.text import (
    description_row_has_successful_text,
    successful_description_text_sql,
)

CANONICAL_ROW_POLICY_VERSION = 4
_POLICY_TEXT = (
    "key=(osm_type,osm_id);winner=max(version);then=max(timestamp);"
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


def _timestamp_rank(value: object) -> float:
    parsed = _parse_timestamp(value)
    if parsed is None:
        return 0.0
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


def select_canonical_row(
    rows: Sequence[Mapping[str, object]],
    *,
    require_successful_text: bool = False,
) -> Mapping[str, object]:
    """Select the stable canonical row for one OSM identity group.

    Text-aware callers filter before ranking, matching the SQL view's
    ``require_successful_text`` behavior.
    """
    candidates = (
        tuple(row for row in rows if description_row_has_successful_text(row))
        if require_successful_text
        else rows
    )
    if not candidates:
        raise ValueError("cannot select a canonical row from an empty group")
    return min(
        candidates,
        key=lambda row: (
            -_version(row.get("version")),
            -_timestamp_rank(row.get("timestamp")),
            str(row.get("source_pbf", "")),
            _row_fingerprint(row),
        ),
    )


def _sql_fingerprint_value(column: str, *, key_value_columns_are_maps: bool) -> str:
    quoted = f'"{column}"'
    if column == "geometry":
        return f"lower(hex({canonical_geometry_wkb_sql(quoted)}))"
    if column in KEY_VALUE_COLUMNS:
        entries = f"map_entries({quoted})" if key_value_columns_are_maps else quoted
        return f"list_sort({entries})"
    return quoted


def _full_row_fingerprint_sql(*, key_value_columns_are_maps: bool = False) -> str:
    fields = ", ".join(
        f"'{column}', "
        f"{_sql_fingerprint_value(column, key_value_columns_are_maps=key_value_columns_are_maps)}"
        for column in sorted(CANONICAL_FINGERPRINT_COLUMNS)
    )
    return f"sha256(json_object({fields}))"


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


__all__ = [
    "CANONICAL_FINGERPRINT_COLUMNS",
    "CANONICAL_RANK_COLUMNS",
    "CANONICAL_ROW_POLICY_SHA256",
    "CANONICAL_ROW_POLICY_VERSION",
    "canonical_geometry_wkb",
    "canonical_geometry_wkb_sql",
    "canonical_row_order_sql",
    "canonical_rows_sql",
    "select_canonical_row",
]
