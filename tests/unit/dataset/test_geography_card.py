"""Dataset-card integration tests for the H3 density map.

These tests prove that:

* the map block is added exactly once;
* the relative asset reference is correct for the Hugging Face repository;
* regenerating the card with unchanged map bytes does not modify the
  surrounding prose (byte-level regression);
* existing generated stats remain unchanged by the map addition;
* repeated regeneration does not insert a duplicate block.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from shapely.geometry import Polygon

from osm_polygon_description_tag._resources import dataset_card_template
from osm_polygon_description_tag.dataset.geography import (
    H3_MAP_ASSET_RELATIVE_PATH,
    H3_MAP_END_MARKER,
    H3_MAP_START_MARKER,
    H3_MAP_TITLE,
    install_map_block,
    render_map_block,
)
from osm_polygon_description_tag.dataset.manifest import (
    Manifest,
    RunCounts,
    current_area_policy_sha256,
    current_output_algorithm_revision,
    output_identity_for,
    source_identity_for,
    write_manifest,
)
from osm_polygon_description_tag.dataset.reporting import generate_dataset_docs
from osm_polygon_description_tag.dataset.storage import write_geoparquet
from osm_polygon_description_tag.workflow.orchestrator import _build_metadata_only_upload_plan
from tests.conftest import make_record_dict


def _populate_dataset(data_root: Path, source_root: Path) -> None:
    (data_root / "data").mkdir(parents=True)
    (data_root / "manifests").mkdir(parents=True)
    source_root.mkdir(exist_ok=True)
    for stem, osm_id in [("alpha", 1), ("beta", 2)]:
        source = source_root / f"{stem}.osm.pbf"
        source.write_bytes(stem.encode("utf-8"))
        record = make_record_dict(
            Polygon([(0, 0), (0, 1), (1, 1), (1, 0)]),
            {"description": stem},
            osm_id=osm_id,
            source_pbf=source.name,
        )
        output = data_root / "data" / f"{stem}.parquet"
        write_geoparquet(iter([record]), output, batch_size=10)
        write_manifest(
            Manifest(
                manifest_schema_version=2,
                schema_version=2,
                geoparquet_version="1.1.0",
                transform_algorithm_version=2,
                area_policy_sha256=current_area_policy_sha256(),
                output_algorithm_revision=current_output_algorithm_revision(),
                source=source_identity_for(source),
                output=output_identity_for(output),
                osmium_version=None,
                dependency_versions={"pyarrow": "20.0.0"},
                code_revision=None,
                started_at="2026-01-01T00:00:00+00:00",
                completed_at="2026-01-01T00:00:01+00:00",
                counts=RunCounts(emitted_features=1, included_rows=1, rejections={}),
            ),
            data_root / "manifests" / f"{stem}.manifest.json",
        )


def _stub_map_block(stats_sha256: str, total_rows: int, occupied_cells: int) -> str:
    """Return a deterministic map body (no markers) for unit tests."""
    return f"![H3 density of description-tagged polygons]({H3_MAP_ASSET_RELATIVE_PATH})\n"


# ---------------------------------------------------------------------------
# Marker contract
# ---------------------------------------------------------------------------


def test_install_map_block_inserts_single_block() -> None:
    template = "before\n\n<!-- GENERATED:H3_MAP:START -->\n<!-- GENERATED:H3_MAP:END -->\n\nafter"
    out = install_map_block(template, "![alt](path.png)")
    assert out.count(H3_MAP_START_MARKER) == 1
    assert out.count(H3_MAP_END_MARKER) == 1


def test_install_map_block_is_idempotent() -> None:
    template = "before\n\n<!-- GENERATED:H3_MAP:START -->\n<!-- GENERATED:H3_MAP:END -->\n\nafter"
    once = install_map_block(template, "![alt](path.png)")
    twice = install_map_block(once, "![alt](path.png)")
    assert once == twice
    assert twice.count(H3_MAP_START_MARKER) == 1

    # Calling with a different body replaces the body but does not duplicate.
    thrice = install_map_block(once, "![alt2](path2.png)")
    assert thrice.count(H3_MAP_START_MARKER) == 1
    assert "path2.png" in thrice


def test_install_map_block_preserves_surrounding_text() -> None:
    template = (
        "header\n"
        "\n"
        "<!-- GENERATED:STATS:START -->\n"
        "stats content\n"
        "<!-- GENERATED:STATS:END -->\n"
        "\n"
        "## Methods\n"
        "\n"
        "Some prose.\n"
        "\n"
        "<!-- GENERATED:H3_MAP:START -->\n"
        "<!-- GENERATED:H3_MAP:END -->\n"
        "\n"
        "footer\n"
    )
    out = install_map_block(template, "![alt](map.png)")
    # Everything outside the map block must be byte-identical.
    start = out.index(H3_MAP_START_MARKER)
    end = out.index(H3_MAP_END_MARKER) + len(H3_MAP_END_MARKER)
    outside = out[:start] + out[end:]
    outside_template = (
        template[: template.index(H3_MAP_START_MARKER)]
        + template[template.index(H3_MAP_END_MARKER) + len(H3_MAP_END_MARKER) :]
    )
    assert outside == outside_template


def test_install_map_block_rejects_template_without_markers() -> None:
    template = "no markers here"
    with pytest.raises(ValueError, match="markers"):
        install_map_block(template, "![alt](map.png)")


def test_install_map_block_rejects_duplicate_markers() -> None:
    template = (
        "<!-- GENERATED:H3_MAP:START -->\n"
        "<!-- GENERATED:H3_MAP:END -->\n"
        "<!-- GENERATED:H3_MAP:START -->\n"
        "<!-- GENERATED:H3_MAP:END -->\n"
    )
    with pytest.raises(ValueError, match="unique"):
        install_map_block(template, "![alt](map.png)")


def test_render_map_block_uses_relative_asset_path() -> None:
    block = render_map_block()
    assert H3_MAP_ASSET_RELATIVE_PATH in block
    assert H3_MAP_START_MARKER in block
    assert H3_MAP_END_MARKER in block


def test_h3_map_title_is_set() -> None:
    assert H3_MAP_TITLE
    assert "H3" in H3_MAP_TITLE


# ---------------------------------------------------------------------------
# End-to-end card integration
# ---------------------------------------------------------------------------


def test_generation_installs_map_block_with_correct_relative_path(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The dataset card template carries the H3 map block at the right place."""
    data_root = tmp_path / "generated"
    source_root = tmp_path / "raw"
    _populate_dataset(data_root, source_root)

    # Patch the rendering helper used inside generate_dataset_docs to a
    # deterministic stub so the test does not require matplotlib.
    monkeypatch.setattr(
        "osm_polygon_description_tag.dataset.reporting._render_h3_map_block",
        lambda data_root, total_rows, occupied_cells: _stub_map_block(
            "deadbeef", total_rows, occupied_cells
        ),
        raising=False,
    )
    # Patch the dataset.reporting module to also use a stub for the PNG.
    monkeypatch.setattr(
        "osm_polygon_description_tag.dataset.reporting._write_h3_map_png",
        lambda data_root, total_rows, occupied_cells: None,
        raising=False,
    )

    generate_dataset_docs(
        data_root, dataset_card_template(), clock=lambda: "2026-01-01T00:00:00+00:00"
    )
    readme = (data_root / "README.md").read_text(encoding="utf-8")
    assert readme.count(H3_MAP_START_MARKER) == 1
    assert readme.count(H3_MAP_END_MARKER) == 1
    assert H3_MAP_ASSET_RELATIVE_PATH in readme
    # The map asset path is relative to the dataset repository root.
    assert not H3_MAP_ASSET_RELATIVE_PATH.startswith("/")


def test_generation_preserves_existing_stats_block(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The map addition must not perturb the existing stats block."""
    data_root = tmp_path / "generated"
    source_root = tmp_path / "raw"
    _populate_dataset(data_root, source_root)

    monkeypatch.setattr(
        "osm_polygon_description_tag.dataset.reporting._render_h3_map_block",
        lambda data_root, total_rows, occupied_cells: _stub_map_block(
            "deadbeef", total_rows, occupied_cells
        ),
        raising=False,
    )
    monkeypatch.setattr(
        "osm_polygon_description_tag.dataset.reporting._write_h3_map_png",
        lambda data_root, total_rows, occupied_cells: None,
        raising=False,
    )

    generate_dataset_docs(data_root, dataset_card_template())
    readme = (data_root / "README.md").read_text(encoding="utf-8")

    start_stats = readme.index("<!-- GENERATED:STATS:START -->")
    end_stats = readme.index("<!-- GENERATED:STATS:END -->") + len("<!-- GENERATED:STATS:END -->")
    start_map = readme.index("<!-- GENERATED:H3_MAP:START -->")
    end_map = readme.index("<!-- GENERATED:H3_MAP:END -->") + len("<!-- GENERATED:H3_MAP:END -->")

    # The two generated blocks must be independent and complete.
    assert start_map > end_stats
    stats_block = readme[start_stats:end_stats]
    map_block = readme[start_map:end_map]
    assert "## Dataset at a glance" in stats_block
    assert H3_MAP_ASSET_RELATIVE_PATH in map_block


def test_byte_level_regression_only_map_block_changes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Regenerating the card changes only the map block; everything else is verbatim."""
    data_root = tmp_path / "generated"
    source_root = tmp_path / "raw"
    _populate_dataset(data_root, source_root)
    template = dataset_card_template()

    # First generation: install the map block with text A.
    monkeypatch.setattr(
        "osm_polygon_description_tag.dataset.reporting._render_h3_map_block",
        lambda data_root, total_rows, occupied_cells: "![alt version A](map.png)\n",
        raising=False,
    )
    monkeypatch.setattr(
        "osm_polygon_description_tag.dataset.reporting._write_h3_map_png",
        lambda data_root, total_rows, occupied_cells: None,
        raising=False,
    )
    generate_dataset_docs(data_root, template)
    readme_a = (data_root / "README.md").read_text(encoding="utf-8")

    # Second generation: same surrounding prose, different map text.
    monkeypatch.setattr(
        "osm_polygon_description_tag.dataset.reporting._render_h3_map_block",
        lambda data_root, total_rows, occupied_cells: "![alt version B](map.png)\n",
        raising=False,
    )
    generate_dataset_docs(data_root, template)
    readme_b = (data_root / "README.md").read_text(encoding="utf-8")

    # Extract the area outside the map block; it must be identical.
    a_start = readme_a.index(H3_MAP_START_MARKER)
    a_end = readme_a.index(H3_MAP_END_MARKER) + len(H3_MAP_END_MARKER)
    b_start = readme_b.index(H3_MAP_START_MARKER)
    b_end = readme_b.index(H3_MAP_END_MARKER) + len(H3_MAP_END_MARKER)
    outside_a = readme_a[:a_start] + readme_a[a_end:]
    outside_b = readme_b[:b_start] + readme_b[b_end:]
    assert outside_a == outside_b


def test_idempotent_regeneration_does_not_duplicate_map_block(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Repeated regeneration never inserts a second map block."""
    data_root = tmp_path / "generated"
    source_root = tmp_path / "raw"
    _populate_dataset(data_root, source_root)

    monkeypatch.setattr(
        "osm_polygon_description_tag.dataset.reporting._render_h3_map_block",
        lambda data_root, total_rows, occupied_cells: _stub_map_block(
            "deadbeef", total_rows, occupied_cells
        ),
        raising=False,
    )
    monkeypatch.setattr(
        "osm_polygon_description_tag.dataset.reporting._write_h3_map_png",
        lambda data_root, total_rows, occupied_cells: None,
        raising=False,
    )
    for _ in range(3):
        generate_dataset_docs(data_root, dataset_card_template())
    readme = (data_root / "README.md").read_text(encoding="utf-8")
    assert readme.count(H3_MAP_START_MARKER) == 1
    assert readme.count(H3_MAP_END_MARKER) == 1


def test_metadata_only_plan_includes_map_when_present(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The metadata plan contains the map when assets/description_polygon_density.png exists."""
    data_root = tmp_path / "generated"
    source_root = tmp_path / "raw"
    _populate_dataset(data_root, source_root)
    (data_root / "README.md").write_text("# README")
    (data_root / "stats.json").write_text("{}")
    (data_root / "assets").mkdir()
    (data_root / "assets" / "description_polygon_density.png").write_bytes(b"\x89PNG\r\n\x1a\n")
    plan = _build_metadata_only_upload_plan(data_root)
    relative = sorted(item.relative_path for item in plan.files)
    assert "assets/description_polygon_density.png" in relative
