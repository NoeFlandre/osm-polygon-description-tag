"""Shared deterministic canonical-row selection policy."""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping, Sequence
from datetime import UTC, datetime

from osm_polygon_description_tag.dataset.schema import SCHEMA

CANONICAL_ROW_POLICY_VERSION = 2
_POLICY_TEXT = (
    "key=(osm_type,osm_id);winner=max(version);then=max(timestamp);"
    "then=min(source_pbf);then=min(full_row_fingerprint)"
)
CANONICAL_ROW_POLICY_SHA256 = hashlib.sha256(_POLICY_TEXT.encode("utf-8")).hexdigest()

CANONICAL_RANK_COLUMNS = (
    "source_pbf",
    "osm_type",
    "osm_id",
    "version",
    "timestamp",
)
CANONICAL_FINGERPRINT_COLUMNS = tuple(SCHEMA.names)


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


def _row_fingerprint(row: Mapping[str, object]) -> str:
    payload = json.dumps(
        {key: row.get(key) for key in SCHEMA.names if key != "source_pbf"},
        ensure_ascii=False,
        sort_keys=True,
        default=str,
        separators=(",", ":"),
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def select_canonical_row(rows: Sequence[Mapping[str, object]]) -> Mapping[str, object]:
    """Select the stable canonical row for one OSM identity group."""
    if not rows:
        raise ValueError("cannot select a canonical row from an empty group")
    return min(
        rows,
        key=lambda row: (
            -_version(row.get("version")),
            -_timestamp_rank(row.get("timestamp")),
            str(row.get("source_pbf", "")),
            _row_fingerprint(row),
        ),
    )


def _full_row_fingerprint_sql() -> str:
    fields = ", ".join(f'"{column}" := "{column}"' for column in CANONICAL_FINGERPRINT_COLUMNS)
    return f"md5(to_json(struct_pack({fields})))"


def canonical_row_order_sql() -> str:
    """Return the SQL order expression implementing the canonical policy."""
    return (
        "version DESC NULLS LAST, "
        "timestamp DESC NULLS LAST, "
        "source_pbf ASC, "
        f"{_full_row_fingerprint_sql()} ASC"
    )


def canonical_rows_sql(relation: str, columns: Sequence[str]) -> str:
    """Return one canonical row per ``(osm_type, osm_id)`` from a relation."""
    selected = tuple(dict.fromkeys(columns))
    if not selected:
        raise ValueError("unique-row views require at least one selected column")
    unknown = set(selected) - set(SCHEMA.names)
    if unknown:
        raise ValueError(f"unsupported unique-row columns: {sorted(unknown)}")
    selected_sql = ", ".join(selected)
    return f"""
        SELECT {selected_sql}
        FROM (
            SELECT *, ROW_NUMBER() OVER (
                PARTITION BY osm_type, osm_id
                ORDER BY {canonical_row_order_sql()}
            ) AS _canonical_rank
            FROM {relation}
        ) ranked
        WHERE _canonical_rank = 1
    """  # noqa: S608 - relation/columns are internal allowlisted SQL fragments


__all__ = [
    "CANONICAL_FINGERPRINT_COLUMNS",
    "CANONICAL_RANK_COLUMNS",
    "CANONICAL_ROW_POLICY_SHA256",
    "CANONICAL_ROW_POLICY_VERSION",
    "canonical_row_order_sql",
    "canonical_rows_sql",
    "select_canonical_row",
]
