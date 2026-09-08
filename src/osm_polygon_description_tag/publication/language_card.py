"""Controlled, formatting-preserving installation of the language card addition."""

from __future__ import annotations

import re
from collections.abc import Mapping
from typing import Any, Final, cast

import yaml
from yaml.nodes import MappingNode, ScalarNode, SequenceNode
from yaml.resolver import BaseResolver

from osm_polygon_description_tag.publication.language import (
    LANGUAGE_CONFIG_NAME,
    LanguageExport,
    LanguagePublicationError,
    render_language_card_section,
)

LANGUAGE_CARD_SECTION_START: Final = "<!-- GENERATED:LANGUAGE_V1:START -->"
LANGUAGE_CARD_SECTION_END: Final = "<!-- GENERATED:LANGUAGE_V1:END -->"

_SECTION_HEADING = f"## Language annotations (`{LANGUAGE_CONFIG_NAME}`)"
_FRONT_MATTER_OPEN = re.compile(r"\A---[ \t]*(?P<newline>\r?\n)")
_FRONT_MATTER_CLOSE = re.compile(r"^---[ \t]*(?:\r?\n|\Z)", re.MULTILINE)


class _UniqueKeyLoader(yaml.SafeLoader):
    """Safe YAML loader that does not silently accept duplicate keys."""


def _construct_unique_mapping(loader: Any, node: Any, deep: bool = False) -> dict[object, object]:
    mapping: dict[object, object] = {}
    for key_node, value_node in node.value:
        key = loader.construct_object(key_node, deep=deep)
        if key in mapping:
            raise yaml.YAMLError(f"duplicate YAML key: {key!r}")
        mapping[key] = loader.construct_object(value_node, deep=deep)
    return mapping


_UniqueKeyLoader.add_constructor(BaseResolver.DEFAULT_MAPPING_TAG, _construct_unique_mapping)


def _card_error(detail: str) -> LanguagePublicationError:
    return LanguagePublicationError(f"dataset card {detail}")


def _front_matter(readme: str) -> tuple[str, str, int, int]:
    opening = _FRONT_MATTER_OPEN.match(readme)
    if opening is None:
        raise _card_error("must start with YAML front matter")
    content_start = opening.end()
    closing = _FRONT_MATTER_CLOSE.search(readme, content_start)
    if closing is None:
        raise _card_error("has no closing YAML front matter delimiter")
    content_end = closing.start()
    content = readme[content_start:content_end]
    if not content.strip():
        raise _card_error("front matter is empty")
    return content, opening.group("newline"), content_start, content_end


def _parse_configs(front_matter: str) -> tuple[SequenceNode, list[dict[str, object]]]:
    try:
        payload, root = _load_front_matter(front_matter)
    except (TypeError, ValueError, yaml.YAMLError) as error:
        raise _card_error(f"front matter is malformed: {error}") from error
    payload, root = _require_mapping_front_matter(payload, root)
    return _parse_config_entries(payload, root)


def _require_mapping_front_matter(
    payload: object, root: object
) -> tuple[Mapping[object, object], MappingNode]:
    if not isinstance(payload, Mapping) or not isinstance(root, MappingNode):
        raise _card_error("front matter must be a mapping")
    return cast(Mapping[object, object], payload), root


def _parse_config_entries(
    payload: Mapping[object, object], root: MappingNode
) -> tuple[SequenceNode, list[dict[str, object]]]:
    config_node = _config_node(root)
    configs = payload.get("configs")
    if not isinstance(configs, list) or not configs:
        raise _card_error("must contain a non-empty configs list")
    entries = _config_entries(cast(list[object], configs))
    return config_node, entries


def _load_front_matter(front_matter: str) -> tuple[object, object]:
    loader = _UniqueKeyLoader(front_matter)
    try:
        payload = loader.get_single_data()
    finally:
        loader.dispose()
    return payload, yaml.compose(front_matter, Loader=yaml.SafeLoader)


def _config_node(root: MappingNode) -> SequenceNode:
    for key_node, value_node in root.value:
        if isinstance(key_node, ScalarNode) and key_node.value == "configs":
            if isinstance(value_node, SequenceNode):
                return value_node
            break
    raise _card_error("must contain a configs sequence")


def _config_entries(configs: list[object]) -> list[dict[str, object]]:
    entries: list[dict[str, object]] = []
    names: set[str] = set()
    for entry in configs:
        entries.append(_config_entry(entry, names))
    return entries


def _config_entry(entry: object, names: set[str]) -> dict[str, object]:
    if not isinstance(entry, Mapping):
        raise _card_error("configs entries must be mappings")
    name = entry.get("config_name")
    if not isinstance(name, str) or not name:
        raise _card_error("configs entries need a non-empty config_name")
    if name in names:
        raise _card_error(f"contains duplicate configuration {name!r}")
    names.add(name)
    return dict(cast(Mapping[str, object], entry))


def _language_config(export: LanguageExport) -> dict[str, object]:
    return {
        "config_name": LANGUAGE_CONFIG_NAME,
        "data_files": [{"split": "train", "path": sorted(export.files)}],
    }


def _install_config(
    front_matter: str,
    config_node: SequenceNode,
    configs: list[dict[str, object]],
    export: LanguageExport,
    newline: str,
) -> str:
    config = _language_config(export)
    if _has_language_config(configs, config):
        return front_matter

    entry_text = yaml.safe_dump(
        [config], default_flow_style=False, sort_keys=False, allow_unicode=True
    ).rstrip("\n")
    return _insert_config(front_matter, config_node, entry_text, config, newline)


def _insert_config(
    front_matter: str,
    config_node: SequenceNode,
    entry_text: str,
    config: Mapping[str, object],
    newline: str,
) -> str:
    start_mark = cast(Any, config_node.start_mark)
    end_mark = cast(Any, config_node.end_mark)
    if config_node.flow_style:
        return _install_flow_config(front_matter, end_mark.index - 1, config)
    return _install_block_config(
        front_matter,
        start_mark.column,
        end_mark.index,
        entry_text,
        newline,
    )


def _install_block_config(
    front_matter: str, column: int, position: int, entry_text: str, newline: str
) -> str:
    indent = " " * column
    entry = newline.join(f"{indent}{line}" for line in entry_text.splitlines())
    before = front_matter[:position]
    after = front_matter[position:]
    before_separator = "" if before.endswith(("\n", "\r")) else newline
    after_separator = "" if after.startswith(("\n", "\r")) else newline
    return before + before_separator + entry + after_separator + after


def _has_language_config(configs: list[dict[str, object]], expected: Mapping[str, object]) -> bool:
    matches = [entry for entry in configs if entry.get("config_name") == LANGUAGE_CONFIG_NAME]
    return _valid_language_config(matches, expected)


def _valid_language_config(
    matches: list[dict[str, object]], expected: Mapping[str, object]
) -> bool:
    if not matches:
        return False
    if matches[0] != expected:
        raise _card_error("contains a conflicting language-v1 configuration")
    return True


def _install_flow_config(front_matter: str, position: int, config: Mapping[str, object]) -> str:
    """Append the controlled mapping to a valid flow-style configs list."""
    before = front_matter[:position]
    after = front_matter[position:]
    inline = yaml.safe_dump(
        config, default_flow_style=True, sort_keys=False, width=1000, allow_unicode=True
    ).strip()
    trimmed = before.rstrip(" \t")
    separator = _flow_separator(before, trimmed)
    return before + separator + inline + after


def _flow_separator(before: str, trimmed: str) -> str:
    if trimmed.endswith(("[", ",")):
        return " " if before != trimmed else ""
    return ", "


def _section_block(export: LanguageExport, newline: str) -> str:
    section = render_language_card_section(export).rstrip("\r\n")
    if newline != "\n":
        section = section.replace("\n", newline)
    return (
        f"{LANGUAGE_CARD_SECTION_START}{newline}"
        f"{section}{newline}"
        f"{LANGUAGE_CARD_SECTION_END}{newline}"
    )


def _append_section(readme: str, block: str, newline: str) -> str:
    if readme.endswith(newline + newline):
        separator = ""
    elif readme.endswith(newline):
        separator = newline
    else:
        separator = newline + newline
    return readme + separator + block


def _replace_section(readme: str, block: str) -> str:
    start, end = _marker_offsets(readme)
    _require_marker_lines(readme, start, end)
    end_after = end + len(LANGUAGE_CARD_SECTION_END)
    heading_start = readme.find(_SECTION_HEADING, start, end)
    if heading_start < 0:
        raise _card_error("language-v1 section markers do not contain the section heading")
    end_after = _consume_line_ending(readme, end_after)
    return readme[:start] + block + readme[end_after:]


def _marker_offsets(readme: str) -> tuple[int, int]:
    start = readme.find(LANGUAGE_CARD_SECTION_START)
    end = readme.find(LANGUAGE_CARD_SECTION_END)
    if start < 0 or end < 0 or end < start:
        raise _card_error("has malformed language-v1 section markers")
    return start, end


def _require_marker_lines(readme: str, start: int, end: int) -> None:
    if not _line_prefix(readme, start) or not _line_prefix(readme, end):
        raise _card_error("language-v1 section markers must occupy complete lines")
    start_after = start + len(LANGUAGE_CARD_SECTION_START)
    end_after = end + len(LANGUAGE_CARD_SECTION_END)
    if not _line_suffix(readme, start_after) or not _line_suffix(readme, end_after):
        raise _card_error("language-v1 section markers must occupy complete lines")


def _line_prefix(readme: str, position: int) -> bool:
    line_start = readme.rfind("\n", 0, position) + 1
    return readme[line_start:position] in ("", "\r")


def _line_suffix(readme: str, position: int) -> bool:
    line_end = readme.find("\n", position)
    tail = readme[position:] if line_end < 0 else readme[position:line_end]
    return tail in ("", "\r")


def _consume_line_ending(readme: str, position: int) -> int:
    if readme.startswith("\r\n", position):
        return position + 2
    if readme.startswith("\n", position):
        return position + 1
    return position


def _install_section(readme: str, export: LanguageExport, newline: str) -> str:
    starts = readme.count(LANGUAGE_CARD_SECTION_START)
    ends = readme.count(LANGUAGE_CARD_SECTION_END)
    _validate_marker_counts(starts, ends)
    heading_count = readme.count(_SECTION_HEADING)
    block = _section_block(export, newline)
    if starts == 0:
        return _append_unmarked_section(readme, block, newline, heading_count)
    return _replace_marked_section(readme, block, heading_count)


def _validate_marker_counts(starts: int, ends: int) -> None:
    if starts != ends or starts > 1:
        raise _card_error("has malformed language-v1 section markers")


def _append_unmarked_section(readme: str, block: str, newline: str, heading_count: int) -> str:
    if heading_count:
        raise _card_error("contains an unmarked language-v1 card section")
    return _append_section(readme, block, newline)


def _replace_marked_section(readme: str, block: str, heading_count: int) -> str:
    if heading_count != 1:
        raise _card_error("has a malformed language-v1 card section")
    return _replace_section(readme, block)


def install_language_card(readme: str, export: LanguageExport) -> str:
    """Add or refresh the language configuration and generated section.

    The input is treated as an immutable baseline: YAML is validated before
    insertion, existing configuration/data/prose bytes are retained, and only
    this module's marked section may be replaced on a later invocation.
    """
    if export.config_name != LANGUAGE_CONFIG_NAME:
        raise LanguagePublicationError("language export declares a different configuration name")
    front, newline, content_start, content_end = _front_matter(readme)
    config_node, configs = _parse_configs(front)
    new_front = _install_config(front, config_node, configs, export, newline)
    updated = readme[:content_start] + new_front + readme[content_end:]
    return _install_section(updated, export, newline)


__all__ = [
    "LANGUAGE_CARD_SECTION_END",
    "LANGUAGE_CARD_SECTION_START",
    "install_language_card",
]
