import json
from pathlib import Path

import pytest
from shapely.geometry import MultiPolygon, Polygon

from osm_polygon_description_tag.dataset.manifest import (
    Manifest,
    RunCounts,
    output_identity_for,
    source_identity_for,
    write_manifest,
)
from osm_polygon_description_tag.dataset.reporting import collect_stats, generate_dataset_docs
from osm_polygon_description_tag.dataset.storage import write_geoparquet
from tests.conftest import make_record_dict


def _frozen_clock() -> str:
    return "2026-07-27T00:00:00+00:00"


def _build_pair(
    data_root: Path,
    source_root: Path,
    name: str,
    records: list[dict[str, object]],
    rejections: dict[str, int],
) -> None:
    source_path = source_root / f"{name}.osm.pbf"
    source_path.write_bytes(name.encode("utf-8"))
    output_path = data_root / "data" / f"{name}.parquet"
    manifest_path = data_root / "manifests" / f"{name}.manifest.json"
    included = write_geoparquet(iter(records), output_path, batch_size=10)
    manifest = Manifest(
        manifest_schema_version=2,
        schema_version=2,
        geoparquet_version="1.1.0",
        transform_algorithm_version=2,
        output_algorithm_revision="x" * 64,
        area_policy_sha256="0" * 64,
        source=source_identity_for(source_path),
        output=output_identity_for(output_path),
        osmium_version="osmium version 1.16.0",
        dependency_versions={"pyarrow": "20.0.0"},
        code_revision="abc123",
        started_at="2026-07-27T00:00:00+00:00",
        completed_at="2026-07-27T00:01:00+00:00",
        counts=RunCounts(
            emitted_features=included + sum(rejections.values()),
            included_rows=included,
            rejections=rejections,
        ),
    )
    write_manifest(manifest, manifest_path)


def _populate_dataset(data_root: Path, source_root: Path) -> None:
    (data_root / "data").mkdir(parents=True)
    (data_root / "manifests").mkdir(parents=True)
    region_a = [
        make_record_dict(
            Polygon([(0, 0), (0, 1), (1, 1), (1, 0)]),
            {"description:en": "EN"},
            osm_type="way",
            osm_id=1,
            source_pbf="region-a.osm.pbf",
        ),
        make_record_dict(
            MultiPolygon([Polygon([(10, 10), (10, 11), (11, 11), (11, 10)])]),
            {"description:pt-BR": "PT"},
            osm_type="relation",
            osm_id=2,
            source_pbf="region-a.osm.pbf",
        ),
    ]
    region_b = [
        make_record_dict(
            Polygon([(0, 0), (0, 1), (1, 1), (1, 0)]),
            {"description:en": "EN"},
            osm_type="way",
            osm_id=3,
            source_pbf="region-b.osm.pbf",
        )
    ]
    _build_pair(data_root, source_root, "region-a", region_a, {"no_nonempty_description": 2})
    _build_pair(data_root, source_root, "region-b", region_b, {"no_nonempty_description": 2})


def test_collect_stats_aggregates_from_validated_artifacts(tmp_path: Path) -> None:
    data_root = tmp_path / "generated"
    source_root = tmp_path / "raw"
    source_root.mkdir()
    _populate_dataset(data_root, source_root)

    stats = collect_stats(data_root, clock=_frozen_clock)

    assert stats["output_files"] == 2
    assert stats["rows"] == 3
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
    assert stats["stats_schema_version"] == 3


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
    _build_pair(data_root, source_root, "words", records, {})

    stats = collect_stats(data_root)

    assert stats["stats_schema_version"] == 3
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
    _populate_dataset(data_root, source_root)
    # Corrupt one output after the manifest was written.
    (data_root / "data" / "region-a.parquet").write_bytes(b"mutated")

    with pytest.raises(ValueError, match="stale"):
        collect_stats(data_root, clock=_frozen_clock)


def test_generate_dataset_docs_writes_stats_and_card(tmp_path: Path) -> None:
    data_root = tmp_path / "generated"
    source_root = tmp_path / "raw"
    source_root.mkdir()
    _populate_dataset(data_root, source_root)
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
    assert stats["rejections"] == {"no_nonempty_description": 4}
