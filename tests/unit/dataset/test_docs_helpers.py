from __future__ import annotations

import hashlib
import json
from pathlib import Path
from unittest.mock import Mock, call, patch

import pytest

import osm_polygon_description_tag.dataset.docs as docs_module


def test_read_json_object_accepts_only_json_objects_and_uses_utf8() -> None:
    path = Mock()
    path.read_text.return_value = '{"answer": 42}'

    assert docs_module._read_json_object(path) == {"answer": 42}
    path.read_text.assert_called_once_with(encoding="utf-8")


@pytest.mark.parametrize(
    "content",
    ["[]", "not-json"],
)
def test_read_json_object_returns_empty_for_non_objects_or_invalid_json(
    tmp_path: Path,
    content: str,
) -> None:
    path = tmp_path / "cache.json"
    path.write_text(content, encoding="utf-8")

    assert docs_module._read_json_object(path) == {}
    assert docs_module._read_json_object(tmp_path / "missing.json") == {}


def test_h3_map_input_hash_defaults_to_no_files_and_uses_canonical_json_options(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    basemap = tmp_path / "basemap.geojson"
    monkeypatch.setattr(docs_module, "bundled_basemap_path", lambda: basemap)
    monkeypatch.setattr(docs_module, "file_sha256", lambda _path: "basemap-sha")

    with patch.object(docs_module.json, "dumps", wraps=json.dumps) as dumps:
        actual = docs_module._h3_map_input_sha256({})

    expected_payload = {
        "cache_schema_version": docs_module._H3_MAP_CACHE_SCHEMA_VERSION,
        "render_version": docs_module._H3_MAP_RENDER_VERSION,
        "h3_resolution": docs_module.DEFAULT_H3_RESOLUTION,
        "basemap_sha256": "basemap-sha",
        "files": [],
    }
    dumps.assert_called_once_with(
        expected_payload,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    expected = hashlib.sha256(
        json.dumps(
            expected_payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")
        ).encode()
    ).hexdigest()
    assert actual == expected


def test_area_histogram_input_hash_defaults_to_empty_file_mapping() -> None:
    with patch.object(docs_module, "area_histogram_input_sha256", return_value="hash") as identity:
        assert docs_module._area_histogram_input_sha256({}) == "hash"

    identity.assert_called_once_with({})


def test_atomic_write_if_changed_creates_missing_nested_parents_and_reuses_them(
    tmp_path: Path,
) -> None:
    target = tmp_path / "nested" / "directory" / "output.bin"

    assert docs_module._atomic_write_if_changed(target, b"first") is True
    target.write_bytes(b"different")
    assert docs_module._atomic_write_if_changed(target, b"second") is True
    assert target.read_bytes() == b"second"


def test_cached_h3_occupied_cells_requires_matching_file_hash_and_valid_count(
    tmp_path: Path,
) -> None:
    map_path = tmp_path / "assets" / "map.png"
    previous = {"h3_map_input_sha256": "same", "h3_occupied_cells": 0}

    assert docs_module._cached_h3_occupied_cells(map_path, previous, "same") is None
    map_path.parent.mkdir()
    map_path.write_bytes(b"map")
    assert docs_module._cached_h3_occupied_cells(map_path, previous, "different") is None
    assert docs_module._cached_h3_occupied_cells(map_path, previous, "same") == 0
    assert (
        docs_module._cached_h3_occupied_cells(
            map_path,
            {"h3_map_input_sha256": "same", "h3_occupied_cells": True},
            "same",
        )
        is None
    )
    assert (
        docs_module._cached_h3_occupied_cells(
            map_path,
            {"h3_map_input_sha256": "same", "h3_occupied_cells": -1},
            "same",
        )
        is None
    )


def test_is_nonnegative_int_rejects_bool_and_non_integer_values() -> None:
    assert docs_module._is_nonnegative_int(0) is True
    assert docs_module._is_nonnegative_int(3) is True
    assert docs_module._is_nonnegative_int(-1) is False
    assert docs_module._is_nonnegative_int(True) is False
    assert docs_module._is_nonnegative_int(1.0) is False
    assert docs_module._is_nonnegative_int("1") is False


def test_ensure_h3_map_reuses_valid_cache_or_aggregates_and_forwards_counts(
    tmp_path: Path,
) -> None:
    stats = {"rows": 7}

    with (
        patch.object(docs_module, "_h3_map_input_sha256", return_value="hash") as input_hash,
        patch.object(docs_module, "_cached_h3_occupied_cells", return_value=3) as cached,
        patch.object(docs_module, "aggregate_h3_density") as aggregate,
        patch.object(docs_module, "render_density_map") as write_map,
    ):
        assert docs_module._ensure_h3_map(tmp_path, stats, {}) == ("hash", 3)

    input_hash.assert_called_once_with(stats)
    cached.assert_called_once_with(
        tmp_path / docs_module.H3_MAP_ASSET_RELATIVE_PATH,
        {},
        "hash",
    )
    aggregate.assert_not_called()
    write_map.assert_not_called()

    counts = {"cell-a": 2, "cell-b": 1}
    with (
        patch.object(docs_module, "_h3_map_input_sha256", return_value="new-hash"),
        patch.object(docs_module, "_cached_h3_occupied_cells", return_value=None),
        patch.object(docs_module, "aggregate_h3_density", return_value=counts) as aggregate,
        patch.object(docs_module, "render_density_map") as write_map,
    ):
        assert docs_module._ensure_h3_map(tmp_path, stats, {}) == ("new-hash", 2)

    aggregate.assert_called_once_with(tmp_path)
    write_map.assert_called_once_with(counts, tmp_path / docs_module.H3_MAP_ASSET_RELATIVE_PATH)


@pytest.mark.parametrize(
    ("path_exists", "previous", "expected"),
    [
        (False, {"area_histogram_total_rows": 0}, False),
        (
            True,
            {
                "area_histogram_input_sha256": "other",
                "area_histogram_render_version": docs_module.AREA_HISTOGRAM_RENDER_VERSION,
                "area_histogram_total_rows": 0,
            },
            False,
        ),
        (
            True,
            {
                "area_histogram_input_sha256": "same",
                "area_histogram_render_version": "old",
                "area_histogram_total_rows": 0,
            },
            False,
        ),
        (
            True,
            {
                "area_histogram_input_sha256": "same",
                "area_histogram_render_version": docs_module.AREA_HISTOGRAM_RENDER_VERSION,
                "area_histogram_total_rows": -1,
            },
            False,
        ),
        (
            True,
            {
                "area_histogram_input_sha256": "same",
                "area_histogram_render_version": docs_module.AREA_HISTOGRAM_RENDER_VERSION,
                "area_histogram_total_rows": True,
            },
            False,
        ),
        (
            True,
            {
                "area_histogram_input_sha256": "same",
                "area_histogram_render_version": docs_module.AREA_HISTOGRAM_RENDER_VERSION,
                "area_histogram_total_rows": 0,
            },
            True,
        ),
    ],
)
def test_histogram_cache_validity_checks_every_cache_invariant(
    tmp_path: Path,
    path_exists: bool,
    previous: dict[str, object],
    expected: bool,
) -> None:
    path = tmp_path / "histogram.png"
    if path_exists:
        path.write_bytes(b"histogram")

    assert docs_module._histogram_cache_is_valid(path, previous, "same") is expected


def test_ensure_area_histogram_reuses_or_rebuilds_and_returns_current_row_count(
    tmp_path: Path,
) -> None:
    stats = {"rows": 9}
    with (
        patch.object(
            docs_module, "_area_histogram_input_sha256", return_value="hash"
        ) as input_hash,
        patch.object(docs_module, "_histogram_cache_is_valid", return_value=True) as valid,
        patch.object(docs_module, "aggregate_area_histogram") as aggregate,
        patch.object(docs_module, "render_area_histogram") as write_histogram,
    ):
        assert docs_module._ensure_area_histogram(tmp_path, stats, {}) == ("hash", 9)

    input_hash.assert_called_once_with(stats)
    valid.assert_called_once_with(
        tmp_path / docs_module._AREA_HISTOGRAM_ASSET_RELATIVE_PATH,
        {},
        "hash",
    )
    write_histogram.assert_not_called()
    aggregate.assert_not_called()

    counts = {"1-10 m²": 9}
    with (
        patch.object(docs_module, "_area_histogram_input_sha256", return_value="new-hash"),
        patch.object(docs_module, "_histogram_cache_is_valid", return_value=False),
        patch.object(docs_module, "aggregate_area_histogram", return_value=counts) as aggregate,
        patch.object(docs_module, "render_area_histogram") as write_histogram,
    ):
        assert docs_module._ensure_area_histogram(tmp_path, stats, {}) == ("new-hash", 9)

    aggregate.assert_called_once_with(tmp_path)
    write_histogram.assert_called_once_with(
        counts, tmp_path / docs_module._AREA_HISTOGRAM_ASSET_RELATIVE_PATH
    )


def test_render_h3_map_block_is_a_stable_relative_markdown_reference() -> None:
    assert docs_module._render_h3_map_block() == (
        f"![{docs_module.H3_MAP_TITLE}]({docs_module.H3_MAP_ASSET_RELATIVE_PATH})\n"
    )


def test_render_stats_block_uses_zero_defaults_and_actual_medians() -> None:
    stats = {
        "stats_schema_version": 1,
        "schema_version": 1,
        "rows": 1,
        "output_files": 1,
        "output_bytes_total": 1,
        "deduplicated_rows": 0,
        "osm_types": {},
        "geometry_types": {},
        "base_description_values": 1,
        "base_description_words_total": 3,
        "base_description_words_median": 1.5,
        "localized_description_values": 0,
        "localized_description_words_total": 0,
        "localized_description_words_median": None,
        "description_suffixes": {},
        "data_min_timestamp_utc": None,
        "data_max_timestamp_utc": None,
    }

    rendered = docs_module._render_stats_block(stats, "hash")

    assert "| Closed ways | 0 |" in rendered
    assert "| Relations | 0 |" in rendered
    assert "| Polygon geometries | 0 |" in rendered
    assert "| MultiPolygon geometries | 0 |" in rendered
    assert "| Base descriptions | 1 | 3 | 1.5 |" in rendered


def test_format_bytes_handles_values_above_the_last_named_unit() -> None:
    assert docs_module._fmt_bytes(1024**5) == "1,024.0 TiB"


def test_write_dataset_docs_renders_stats_map_and_canonical_json(
    tmp_path: Path,
) -> None:
    template = tmp_path / "template.md"
    template.write_text(
        "before\n"
        "<!-- GENERATED:STATS:START -->\nold\n<!-- GENERATED:STATS:END -->\n"
        "<!-- GENERATED:H3_MAP:START -->\n<!-- GENERATED:H3_MAP:END -->\n"
        "after\n",
        encoding="utf-8",
    )
    stats = {"z": "é", "a": 1, "rows": 4, "h3_occupied_cells": 2}
    stats_json = json.dumps(stats, ensure_ascii=False, indent=2, sort_keys=True) + "\n"
    expected_hash = hashlib.sha256(stats_json.encode()).hexdigest()

    with (
        patch.object(
            docs_module, "_render_stats_block", return_value="generated stats"
        ) as render_stats,
        patch.object(docs_module, "_render_h3_map_block", return_value="map body") as render_map,
        patch.object(docs_module, "install_map_block", return_value="mapped readme") as install_map,
        patch.object(docs_module, "_write_if_changed") as write_if_changed,
        patch.object(docs_module.json, "dumps", wraps=json.dumps) as dumps,
    ):
        docs_module._write_dataset_docs(tmp_path, template, stats)

    dumps.assert_called_once_with(stats, ensure_ascii=False, indent=2, sort_keys=True)
    render_stats.assert_called_once_with(stats, expected_hash)
    render_map.assert_called_once_with()
    assert install_map.call_args.args[0].count(docs_module.H3_MAP_START_MARKER) == 1
    install_map.assert_called_once_with(install_map.call_args.args[0], "map body")
    assert write_if_changed.call_args_list == [
        call(tmp_path / "stats.json", stats_json),
        call(tmp_path / "README.md", "mapped readme"),
    ]


def test_write_dataset_docs_rejects_templates_without_stats_markers(tmp_path: Path) -> None:
    template = tmp_path / "template.md"
    template.write_text("no generated block", encoding="utf-8")

    with pytest.raises(
        docs_module.ReportingError,
        match=rf"template missing GENERATED:STATS markers: {template}",
    ):
        docs_module._write_dataset_docs(tmp_path, template, {"rows": 0})


def test_write_dataset_docs_requires_both_h3_markers_before_installing_map(
    tmp_path: Path,
) -> None:
    template = tmp_path / "template.md"
    template.write_text(
        "<!-- GENERATED:STATS:START -->\nx\n<!-- GENERATED:STATS:END -->\n"
        "<!-- GENERATED:H3_MAP:START -->\n",
        encoding="utf-8",
    )
    stats = {"rows": 1, "h3_occupied_cells": 1}

    with (
        patch.object(docs_module, "_render_stats_block", return_value="stats"),
        patch.object(docs_module, "install_map_block") as install_map,
        patch.object(docs_module, "_write_if_changed"),
    ):
        docs_module._write_dataset_docs(tmp_path, template, stats)

    install_map.assert_not_called()


def test_generate_dataset_docs_forwards_clock_and_orchestrates_all_outputs(
    tmp_path: Path,
) -> None:
    template = tmp_path / "template.md"
    clock = Mock()
    stats = {"rows": 5}
    previous = {"old": True}

    with (
        patch.object(docs_module, "collect_stats", return_value=stats) as collect,
        patch.object(docs_module, "_read_json_object", return_value=previous) as read_cache,
        patch.object(docs_module, "_ensure_h3_map", return_value=("h3-hash", 3)) as ensure_h3,
        patch.object(
            docs_module,
            "_ensure_area_histogram",
            return_value=("area-hash", 5),
        ) as ensure_area,
        patch.object(docs_module, "_write_dataset_hero") as write_hero,
        patch.object(docs_module, "_write_dataset_docs") as write_docs,
    ):
        result = docs_module.generate_dataset_docs(tmp_path, template, clock=clock)

    collect.assert_called_once_with(tmp_path, clock=clock)
    read_cache.assert_called_once_with(tmp_path / "stats.json")
    ensure_h3.assert_called_once_with(tmp_path, stats, previous)
    ensure_area.assert_called_once_with(tmp_path, stats, previous)
    write_hero.assert_called_once_with(tmp_path)
    write_docs.assert_called_once_with(tmp_path, template, stats)
    assert result == {
        "rows": 5,
        "h3_map_input_sha256": "h3-hash",
        "h3_occupied_cells": 3,
        "area_histogram_input_sha256": "area-hash",
        "area_histogram_render_version": docs_module.AREA_HISTOGRAM_RENDER_VERSION,
        "area_histogram_total_rows": 5,
    }
