import json
from pathlib import Path

import pytest
from shapely.geometry import Polygon

from osm_polygon_description_tag.dataset.reporting import collect_stats, generate_dataset_docs
from tests.conftest import make_record_dict
from tests.helpers.dataset import write_finalized_dataset, write_reporting_fixture


def _frozen_clock() -> str:
    return "2026-07-27T00:00:00+00:00"


def test_collect_stats_aggregates_from_validated_artifacts(tmp_path: Path) -> None:
    data_root = tmp_path / "generated"
    source_root = tmp_path / "raw"
    source_root.mkdir()
    write_reporting_fixture(data_root, source_root)

    stats = collect_stats(data_root, clock=_frozen_clock)

    assert stats["output_files"] == 2
    assert stats["rows"] == 3
    assert stats["unique_osm_objects"] == 3
    assert stats["regional_overlap_duplicate_rows"] == 0
    assert stats["regional_overlap_duplicate_rate"] == 0.0
    assert stats["osm_types"] == {"relation": 1, "way": 2}
    assert stats["geometry_types"] == {"MultiPolygon": 1, "Polygon": 2}
    assert stats["description_suffixes"] == {"en": 2, "pt-BR": 1}
    assert stats["rejections"] == {"no_nonempty_description": 4}
    assert stats["emitted_features"] == 7
    assert stats["base_description_rows"] == 0
    assert stats["localized_description_rows"] == 3
    assert "generation_timestamp_utc" not in stats
    assert stats["area_m2_min_m2"] is not None and stats["area_m2_min_m2"] > 0
    assert stats["area_m2_max_m2"] >= stats["area_m2_min_m2"]
    assert stats["stats_schema_version"] == 6


def test_collect_stats_separates_base_and_localized_description_words(
    tmp_path: Path,
) -> None:
    data_root = tmp_path / "generated"
    source_root = tmp_path / "raw"
    source_root.mkdir()
    (data_root / "data").mkdir(parents=True)
    (data_root / "manifests").mkdir(parents=True)
    records = [
        make_record_dict(
            Polygon([(0, 0), (0, 1), (1, 1), (1, 0)]),
            {
                "description": "Two words",
                "description:en": "three localized words",
                "description:fr": "quatre\u2003mots",
            },
            osm_id=10,
            source_pbf="words.osm.pbf",
        ),
        make_record_dict(
            Polygon([(2, 2), (2, 3), (3, 3), (3, 2)]),
            {"description": "One", "description:en": "single"},
            osm_id=11,
            source_pbf="words.osm.pbf",
        ),
    ]
    write_finalized_dataset(data_root, source_root, {"words": records})

    stats = collect_stats(data_root)

    assert stats["stats_schema_version"] == 6
    assert stats["base_description_values"] == 2
    assert stats["base_description_words_total"] == 3
    assert stats["base_description_words_median"] == 1.5
    assert stats["localized_description_values"] == 3
    assert stats["localized_description_words_total"] == 6
    assert stats["localized_description_words_median"] == 2.0


def test_collect_stats_uses_zero_totals_and_null_medians_for_empty_dataset(
    tmp_path: Path,
) -> None:
    data_root = tmp_path / "generated"
    (data_root / "data").mkdir(parents=True)
    (data_root / "manifests").mkdir(parents=True)

    stats = collect_stats(data_root)

    assert stats["base_description_values"] == 0
    assert stats["base_description_words_total"] == 0
    assert stats["base_description_words_median"] is None
    assert stats["localized_description_values"] == 0
    assert stats["localized_description_words_total"] == 0
    assert stats["localized_description_words_median"] is None


def test_collect_stats_rejects_missing_manifest(tmp_path: Path) -> None:
    data_root = tmp_path / "generated"
    (data_root / "data").mkdir(parents=True)
    (data_root / "manifests").mkdir(parents=True)
    (data_root / "data" / "lonely.parquet").write_bytes(b"x")

    with pytest.raises(ValueError, match="mismatch|missing"):
        collect_stats(data_root, clock=_frozen_clock)


def test_collect_stats_rejects_stale_output(tmp_path: Path) -> None:
    data_root = tmp_path / "generated"
    source_root = tmp_path / "raw"
    source_root.mkdir()
    write_reporting_fixture(data_root, source_root)
    # Corrupt one output after the manifest was written.
    (data_root / "data" / "region-a.parquet").write_bytes(b"mutated")

    with pytest.raises(ValueError, match="stale"):
        collect_stats(data_root, clock=_frozen_clock)


def test_generate_dataset_docs_installs_hero_image(tmp_path: Path) -> None:
    data_root = tmp_path / "generated"
    source_root = tmp_path / "raw"
    source_root.mkdir()
    write_reporting_fixture(data_root, source_root)
    template_path = Path("docs/dataset-card-template.md")

    generate_dataset_docs(data_root, template_path, clock=_frozen_clock)

    hero = data_root / "assets" / "dataset-card-hero.png"
    assert hero.read_bytes() == (Path("assets/dataset-card-hero.png").read_bytes())
    first_mtime = hero.stat().st_mtime_ns

    readme = (data_root / "README.md").read_text(encoding="utf-8")
    assert "![OSM Polygon Description Tag dataset hero](assets/dataset-card-hero.png)" in readme

    # Re-running with identical inputs leaves the hero byte-identical and
    # preserves its on-disk mtime.
    generate_dataset_docs(data_root, template_path, clock=_frozen_clock)
    assert hero.read_bytes() == (Path("assets/dataset-card-hero.png").read_bytes())
    assert hero.stat().st_mtime_ns == first_mtime


def test_generate_dataset_docs_writes_stats_and_card(tmp_path: Path) -> None:
    data_root = tmp_path / "generated"
    source_root = tmp_path / "raw"
    source_root.mkdir()
    write_reporting_fixture(data_root, source_root)
    template_path = Path("docs/dataset-card-template.md")

    generate_dataset_docs(data_root, template_path, clock=_frozen_clock)

    stats_json = (data_root / "stats.json").read_text(encoding="utf-8")
    stats = json.loads(stats_json)
    assert stats["rows"] == 3
    assert json.dumps(stats, sort_keys=True) == json.dumps(json.loads(stats_json), sort_keys=True)

    readme = (data_root / "README.md").read_text(encoding="utf-8")
    assert readme.startswith("---\n")
    assert "pretty_name: OSM Polygon Description Tag" in readme
    assert "license: odbl" in readme
    assert "OpenStreetMap contributors" in readme
    assert "Open Database License" in readme
    assert "<!-- GENERATED:STATS:START -->" in readme
    assert "<!-- GENERATED:STATS:END -->" in readme
    # The total row count appears inside the generated block.
    start = readme.index("<!-- GENERATED:STATS:START -->")
    end = readme.index("<!-- GENERATED:STATS:END -->")
    generated = readme[start:end]
    assert "stats_sha256" in generated
    assert "## Dataset at a glance" in generated
    assert "## Description coverage" in generated
    assert "Base descriptions" in generated
    assert "Localized descriptions" in generated
    assert "Total words" in generated
    assert "Median words per description" in generated
    assert "Detailed machine-readable statistics" in generated
    assert "Files (deterministic, sorted by parquet filename)" not in generated
    assert "Source SHA-256" not in generated
    assert "Transformation rejections by reason" not in generated
    assert stats["files"][0]["source_sha256"]
    assert stats["files"][0]["output_sha256"]
    assert stats["files"][0]["emitted_features"] == 4
    assert stats["files"][0]["rejections"] == {"no_nonempty_description": 2}
    assert stats["rejections"] == {"no_nonempty_description": 4}
