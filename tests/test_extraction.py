import json
from pathlib import Path

import pytest

from osm_polygon_description_tag.extraction import (
    export_command,
    iter_records,
    parse_copy_record,
)

CONFIG_PATH = Path("/repo/config/osmium-export.json")


def test_export_command_has_no_shell_and_uses_pg_copy() -> None:
    command = export_command(
        Path("/input/a.osm.pbf"),
        CONFIG_PATH,
        executable="osmium",
    )
    assert command == (
        "osmium",
        "export",
        "/input/a.osm.pbf",
        "--output-format",
        "pg",
        "--config",
        "/repo/config/osmium-export.json",
        "--output",
        "-",
    )


def test_copy_parser_keeps_geometry_metadata_and_tags_separate() -> None:
    line = (
        b"0103000020E6100000\tway\t42\t3\t99\t"
        b"2026-01-01T00:00:00Z\t"
        b'{"description":"x","__osm_id":"original tag"}\n'
    )
    record = parse_copy_record(line)

    assert record.geometry_ewkb_hex.startswith("0103000020E6100000")
    assert record.osm_type == "way"
    assert record.osm_id == 42
    assert record.version == 3
    assert record.changeset == 99
    assert record.timestamp == "2026-01-01T00:00:00Z"
    assert record.tags == {"description": "x", "__osm_id": "original tag"}


def test_copy_record_decodes_postgres_escapes_in_tag_values() -> None:
    # osmium JSON-encodes tag values, then COPY-escapes the JSON backslashes.
    # Build the wire bytes explicitly to assert correct double-layer decoding.
    value = "a\tb\\c"  # a, TAB, b, BACKSLASH, c
    tags_json = json.dumps({"description": value}, separators=(",", ":"))
    copy_field = tags_json.replace("\\", "\\\\").encode("utf-8")
    line = b"0103\tway\t7\t\\N\t\\N\t\\N\t" + copy_field + b"\n"
    record = parse_copy_record(line)

    assert record.osm_id == 7
    assert record.version is None
    assert record.changeset is None
    assert record.timestamp is None
    assert record.tags == {"description": value}


def test_copy_record_rejects_wrong_field_count() -> None:
    with pytest.raises(ValueError, match="field"):
        parse_copy_record(b"0103\tway\t42\n")


def test_copy_record_rejects_malformed_tag_json() -> None:
    with pytest.raises(ValueError, match="tags"):
        parse_copy_record(b"0103\tway\t42\t1\t1\t2026-01-01T00:00:00Z\t{not json}\n")


def test_iter_records_skips_blank_lines() -> None:
    good = b"0103\tway\t42\t1\t1\t2026-01-01T00:00:00Z\t{}\n"
    stream = [good, b"\n", b"  \n", b"\t\n"]
    records = list(iter_records(stream))

    assert len(records) == 1
    assert records[0].osm_id == 42


def test_iter_records_raises_typed_error_for_malformed_line() -> None:
    stream = [b"0103\tway\tbad\n"]
    with pytest.raises(ValueError, match="line 1"):
        list(iter_records(stream))


def test_export_record_is_immutable_and_frozen() -> None:
    line = b"0103\tway\t1\t1\t1\t2026-01-01T00:00:00Z\t{}\n"
    record = parse_copy_record(line)

    with pytest.raises(AttributeError):
        record.osm_id = 999  # type: ignore[misc]


def test_config_policy_is_versioned_provenance() -> None:
    config = json.loads(Path("config/osmium-export.json").read_text(encoding="utf-8"))

    assert config["attributes"]["type"] == "__osm_type"
    assert config["attributes"]["id"] == "__osm_id"
    assert config["attributes"]["way_nodes"] is False
    assert config["format_options"]["tags_type"] == "json"
    assert "natural=coastline" in config["linear_tags"]
    assert "building" in config["area_tags"]
