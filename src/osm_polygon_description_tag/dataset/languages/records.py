"""Per-description records extracted from Arrow key/value tag lists."""

import hashlib
import json
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from typing import cast

from osm_polygon_description_tag.runtime.validation import required_text


class DescriptionRecordError(ValueError):
    """Raised when an Arrow description-tag record is malformed."""


def _required_text(row: Mapping[str, object], name: str) -> str:
    return required_text(row.get(name), name, error=DescriptionRecordError)


def _required_id(row: Mapping[str, object]) -> int:
    value = row.get("osm_id")
    if type(value) is not int:
        raise DescriptionRecordError("osm_id must be an integer")
    return value


def _row_metadata(row: Mapping[str, object]) -> tuple[str, str, int]:
    return _required_text(row, "source_pbf"), _required_text(row, "osm_type"), _required_id(row)


def _tag_sequence(value: object) -> Sequence[object]:
    if value is None:
        return ()
    value = _arrow_value(value)
    if value is None:
        return ()
    if isinstance(value, str | bytes) or not isinstance(value, Sequence):
        raise DescriptionRecordError("tags must be an Arrow list of key/value structs")
    return value


def _arrow_value(value: object) -> object:
    as_py = getattr(value, "as_py", None)
    if callable(as_py):
        return as_py()
    to_pylist = getattr(value, "to_pylist", None)
    if callable(to_pylist):
        return _unwrap_single_list(to_pylist())
    return value


def _unwrap_single_list(value: object) -> object:
    if (
        isinstance(value, Sequence)
        and len(value) == 1
        and isinstance(value[0], Sequence)
        and not isinstance(value[0], str | bytes)
    ):
        return value[0]
    return value


def _as_python(item: object) -> object:
    as_py = getattr(item, "as_py", None)
    if callable(as_py):
        return as_py()
    return item


def _mapping_pair(item: Mapping[object, object]) -> tuple[object, object]:
    if set(item) != {"key", "value"}:
        raise DescriptionRecordError("tag struct must contain exactly key and value")
    return item["key"], item["value"]


def _sequence_pair(item: object) -> tuple[object, object]:
    if isinstance(item, str | bytes):
        raise DescriptionRecordError("tag entry must be a key/value struct")
    if not isinstance(item, Sequence):
        raise DescriptionRecordError("tag entry must be a key/value struct")
    if len(item) != 2:
        raise DescriptionRecordError("tag entry must contain exactly key and value")
    return item[0], item[1]


def _validated_key_value(key: object, value: object) -> tuple[str, str | None]:
    if not isinstance(key, str) or not key:
        raise DescriptionRecordError("tag key must be a non-empty string")
    if value is not None and not isinstance(value, str):
        raise DescriptionRecordError("tag value must be a string or null")
    return key, value


def _pair_from_item(item: object) -> tuple[str, str | None]:
    item = _as_python(item)
    if isinstance(item, Mapping):
        typed_item = cast(Mapping[object, object], item)  # pragma: no mutate - static narrowing
        key, value = _mapping_pair(typed_item)
    else:
        key, value = _sequence_pair(item)
    return _validated_key_value(key, value)


def _validated_pairs(value: object) -> tuple[tuple[str, str | None], ...]:
    pairs: list[tuple[str, str | None]] = []
    seen: set[str] = set()
    for item in _tag_sequence(value):
        key, tag_value = _pair_from_item(item)
        if key in seen:
            raise DescriptionRecordError(f"duplicate tag key: {key}")
        seen.add(key)
        pairs.append((key, tag_value))
    return tuple(pairs)


def _is_description_key(key: str) -> bool:
    return key == "description" or (
        key.startswith("description:") and len(key) > len("description:")
    )


def _description_pairs(
    pairs: Sequence[tuple[str, str | None]],
) -> tuple[tuple[str, str], ...]:
    selected = [
        (key, value) for key, value in pairs if _is_description_key(key) and value is not None
    ]
    return tuple(sorted(selected))


def _entry_identity(osm_type: str, osm_id: int, tag_key: str, text_sha256: str) -> str:
    # False/None and the unused object-key separator are equivalent for this flat array.
    # pragma: no mutate start - exact ASCII and Unicode identity hashes are tested
    material = json.dumps(
        [osm_type, osm_id, tag_key, text_sha256],
        ensure_ascii=False,
        separators=(",", ":"),
    ).encode()
    # pragma: no mutate end
    return hashlib.sha256(material).hexdigest()


@dataclass(frozen=True, slots=True)
class DescriptionEntry:
    """One exact base or localized description value from one OSM object."""

    source_pbf: str
    osm_type: str
    osm_id: int
    tag_key: str
    original_text: str
    text_sha256: str = field(init=False)
    description_identity: str = field(init=False)

    def __post_init__(self) -> None:
        _validate_entry_fields(self)
        text_sha256 = hashlib.sha256(self.original_text.encode()).hexdigest()
        object.__setattr__(self, "text_sha256", text_sha256)
        object.__setattr__(
            self,
            "description_identity",
            _entry_identity(self.osm_type, self.osm_id, self.tag_key, text_sha256),
        )


def _validate_entry_fields(entry: DescriptionEntry) -> None:
    _required_entry_text(entry.source_pbf, "source_pbf")
    _required_entry_text(entry.osm_type, "osm_type")
    if type(entry.osm_id) is not int:
        raise DescriptionRecordError("osm_id must be an integer")
    if not _is_description_key(entry.tag_key):
        raise DescriptionRecordError("tag_key must be description or description:<suffix>")
    if not isinstance(entry.original_text, str):
        raise DescriptionRecordError("original_text must be a string")


def _required_entry_text(value: object, name: str) -> None:
    if not isinstance(value, str) or not value:
        raise DescriptionRecordError(f"{name} must be a non-empty string")


def extract_description_entries(row: Mapping[str, object]) -> tuple[DescriptionEntry, ...]:
    """Extract every non-null exact description tag from one Arrow row.

    The base ``description`` entry is ordered first, followed by localized
    keys in lexical order. Localized suffixes are opaque and may be
    non-language tags. The input row and its tag list are never modified.
    """
    if not isinstance(row, Mapping):
        raise DescriptionRecordError("row must be a mapping")
    source_pbf, osm_type, osm_id = _row_metadata(row)
    pairs = _description_pairs(_validated_pairs(row.get("tags")))
    return tuple(
        DescriptionEntry(source_pbf, osm_type, osm_id, tag_key, text) for tag_key, text in pairs
    )


__all__ = ["DescriptionEntry", "DescriptionRecordError", "extract_description_entries"]
