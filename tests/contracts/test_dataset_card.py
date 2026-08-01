from pathlib import Path

import pytest
from shapely.geometry import Polygon

from osm_polygon_description_tag.manifest import (
    Manifest,
    RunCounts,
    output_identity_for,
    source_identity_for,
    write_manifest,
)
from osm_polygon_description_tag.reporting import generate_dataset_docs
from osm_polygon_description_tag.storage import write_geoparquet
from tests.conftest import make_record_dict

TEMPLATE = Path("docs/dataset-card-template.md")


def test_template_contains_required_handwritten_sections() -> None:
    text = TEMPLATE.read_text(encoding="utf-8")

    assert text.startswith("---\n")
    assert "pretty_name: OSM Polygon Description Tag" in text
    assert "license: odbl" in text
    assert "<!-- GENERATED:STATS:START -->" in text
    assert "<!-- GENERATED:STATS:END -->" in text
    # Required provenance and obligation text.
    assert "OpenStreetMap contributors" in text
    assert "Open Database License" in text
    assert "ODbL" in text
    assert "overlap" in text.lower()
    assert "language code" in text.lower() or "language" in text.lower()
    assert "Trackio" in text
    assert "osm-polygon-description-tag-trackio" in text
    hero_markdown = "![OSM Polygon Description Tag dataset hero](assets/dataset-card-hero.png)"
    assert hero_markdown in text
    assert text.index(hero_markdown) < text.index("# OSM Polygon Description Tag")
    # The maintained docs mirror must be byte-identical to the packaged copy.
    packaged = (
        Path("src/osm_polygon_description_tag/_data/dataset-card-template.md").read_text(
            encoding="utf-8"
        )
    )
    assert packaged == text


def _populate(tmp_path: Path) -> Path:
    data_root = tmp_path / "generated"
    source_root = tmp_path / "raw"
    (data_root / "data").mkdir(parents=True)
    (data_root / "manifests").mkdir(parents=True)
    source_root.mkdir()
    source = source_root / "a.osm.pbf"
    source.write_bytes(b"a")
    record = make_record_dict(
        Polygon([(0, 0), (0, 1), (1, 1), (1, 0)]),
        {"description": "A place", "description:en": "EN"},
        osm_id=1,
        source_pbf="a.osm.pbf",
    )
    output = data_root / "data" / "a.parquet"
    manifest_path = data_root / "manifests" / "a.manifest.json"
    rows = write_geoparquet(iter([record]), output, batch_size=10)
    write_manifest(
        Manifest(
            manifest_schema_version=2,
            schema_version=2,
            geoparquet_version="1.1.0",
            transform_algorithm_version=2,
            output_algorithm_revision="x" * 64,
            area_policy_sha256="0" * 64,
            source=source_identity_for(source),
            output=output_identity_for(output),
            osmium_version="osmium version 1.16.0",
            dependency_versions={"pyarrow": "20.0.0"},
            code_revision="abc123",
            started_at="2026-07-27T00:00:00+00:00",
            completed_at="2026-07-27T00:01:00+00:00",
            counts=RunCounts(emitted_features=1, included_rows=rows, rejections={}),
        ),
        manifest_path,
    )
    return data_root


def test_generation_preserves_handwritten_sections(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    data_root = _populate(tmp_path)

    # Stub the H3 map hooks so the test focuses on the dataset card
    # generation, not on matplotlib or the assets/ directory.
    monkeypatch.setattr(
        "osm_polygon_description_tag.dataset.reporting._render_h3_map_block",
        lambda data_root,
        total_rows,
        occupied_cells: "![H3 map](assets/description_polygon_density.png)\n",
        raising=False,
    )
    monkeypatch.setattr(
        "osm_polygon_description_tag.dataset.reporting._write_h3_map_png",
        lambda data_root, total_rows, occupied_cells, counts=None: None,
        raising=False,
    )

    generate_dataset_docs(data_root, TEMPLATE, clock=lambda: "2026-07-27T00:00:00+00:00")

    readme = (data_root / "README.md").read_text(encoding="utf-8")
    template = TEMPLATE.read_text(encoding="utf-8")

    def _strip_generated(text: str) -> str:
        for start_marker, end_marker in (
            ("<!-- GENERATED:STATS:START -->", "<!-- GENERATED:STATS:END -->"),
            ("<!-- GENERATED:H3_MAP:START -->", "<!-- GENERATED:H3_MAP:END -->"),
        ):
            start = text.index(start_marker)
            end = text.index(end_marker) + len(end_marker)
            text = text[:start] + text[end:]
        return text

    handwritten_actual = _strip_generated(readme)
    handwritten_template = _strip_generated(template)
    # Only the marked generated blocks change; everything else is verbatim.
    assert handwritten_actual == handwritten_template
    # Attribution and obligations survive generation.
    assert "OpenStreetMap contributors" in readme
    assert "Open Database License" in readme


def test_generated_block_contains_only_backed_numbers(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    data_root = _populate(tmp_path)

    # Stub the H3 map hooks so the test focuses on the dataset card
    # generation, not on matplotlib or the assets/ directory.
    monkeypatch.setattr(
        "osm_polygon_description_tag.dataset.reporting._render_h3_map_block",
        lambda data_root,
        total_rows,
        occupied_cells: "![H3 map](assets/description_polygon_density.png)\n",
        raising=False,
    )
    monkeypatch.setattr(
        "osm_polygon_description_tag.dataset.reporting._write_h3_map_png",
        lambda data_root, total_rows, occupied_cells, counts=None: None,
        raising=False,
    )

    stats = generate_dataset_docs(data_root, TEMPLATE, clock=lambda: "2026-07-27T00:00:00+00:00")

    readme = (data_root / "README.md").read_text(encoding="utf-8")
    start = readme.index("<!-- GENERATED:STATS:START -->")
    end = readme.index("<!-- GENERATED:STATS:END -->")
    generated = readme[start:end]
    assert f"schema_version: {stats['schema_version']}" in generated
    assert "stats_sha256:" in generated
    assert "## Dataset at a glance" in generated
    assert "## Description coverage" in generated
    assert "Base descriptions" in generated
    assert "Localized descriptions" in generated
    assert "Total words" in generated
    assert "Median words per description" in generated
    assert "Detailed machine-readable statistics" in generated
    assert "Files (deterministic, sorted by parquet filename)" not in readme
    assert "Source SHA-256" not in readme
    assert "Transformation rejections by reason" not in readme
    assert len(readme.splitlines()) < 180
    assert stats["files"][0]["source_sha256"]
    assert stats["files"][0]["output_sha256"]
