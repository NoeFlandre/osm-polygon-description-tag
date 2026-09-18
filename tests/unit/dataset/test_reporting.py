import json
from pathlib import Path
from unittest.mock import patch

import pytest
from shapely.geometry import MultiPolygon, Polygon

from osm_polygon_description_tag.dataset.canonical_rows import select_canonical_row
from osm_polygon_description_tag.dataset.geography import (
    aggregate_area_histogram,
    aggregate_h3_density,
)
from osm_polygon_description_tag.dataset.reporting import collect_stats, generate_dataset_docs
from osm_polygon_description_tag.dataset.stats import (
    _collect_feature_summary,
    _collect_manifest_summary,
    _create_feature_table,
    _create_unique_feature_view,
    _find_validated_artifacts,
    _ingest_features,
    _new_connection,
    _validate_artifact,
)
from osm_polygon_description_tag.dataset.storage import StorageError
from osm_polygon_description_tag.dataset.unique_rows import iter_unique_parquet_batches
from tests.conftest import make_record_dict
from tests.helpers.dataset import write_finalized_dataset, write_reporting_fixture


def _repository_file(relative_path: str) -> Path:
    for parent in Path(__file__).resolve().parents:
        candidate = parent / relative_path
        if candidate.is_file():
            return candidate
    raise AssertionError(f"could not find repository file: {relative_path}")


_TEMPLATE_PATH = _repository_file("docs/dataset-card-template.md")


def _frozen_clock() -> str:
    return "2026-07-27T00:00:00+00:00"


def test_reporting_phases_validate_and_summarize_artifacts_in_filename_order(
    tmp_path: Path,
) -> None:
    data_root = tmp_path / "generated"
    source_root = tmp_path / "raw"
    source_root.mkdir()
    write_reporting_fixture(data_root, source_root)

    artifacts = _find_validated_artifacts(data_root)
    assert [artifact.parquet.name for artifact in artifacts] == [
        "region-a.parquet",
        "region-b.parquet",
    ]

    summary = _collect_manifest_summary(artifacts)

    assert summary.emitted_features == 7
    assert summary.rejections == {"no_nonempty_description": 4}
    assert [record["parquet"] for record in summary.files] == [
        "region-a.parquet",
        "region-b.parquet",
    ]


def test_artifact_validation_returns_the_matching_manifest(tmp_path: Path) -> None:
    data_root = tmp_path / "generated"
    source_root = tmp_path / "raw"
    source_root.mkdir()
    write_reporting_fixture(data_root, source_root)

    artifact = _validate_artifact(
        data_root / "data" / "region-a.parquet",
        data_root / "manifests",
    )

    assert artifact.parquet.name == "region-a.parquet"
    assert artifact.manifest.source.name == "region-a.osm.pbf"


def test_reporting_feature_phase_matches_public_stats(tmp_path: Path) -> None:
    data_root = tmp_path / "generated"
    source_root = tmp_path / "raw"
    source_root.mkdir()
    write_reporting_fixture(data_root, source_root)
    artifacts = _find_validated_artifacts(data_root)

    connection = _new_connection(data_root)
    try:
        _create_feature_table(connection)
        _ingest_features(connection, artifacts)
        _create_unique_feature_view(connection)
        summary = _collect_feature_summary(connection)
    finally:
        connection.close()

    assert summary.rows == 3
    assert summary.unique_osm_objects == 3
    assert summary.all_unique_osm_objects == 3
    assert summary.osm_types == {"relation": 1, "way": 2}
    assert summary.geometry_types == {"MultiPolygon": 1, "Polygon": 2}
    assert summary.description_suffixes == {"en": 2, "pt-BR": 1}
    assert summary.base_description_rows == 0
    assert summary.localized_description_rows == 3


def test_collect_stats_aggregates_from_validated_artifacts(tmp_path: Path) -> None:
    data_root = tmp_path / "generated"
    source_root = tmp_path / "raw"
    source_root.mkdir()
    write_reporting_fixture(data_root, source_root)

    stats = collect_stats(data_root, clock=_frozen_clock)

    assert stats["output_files"] == 2
    assert stats["rows"] == 3
    assert stats["unique_osm_objects"] == 3
    assert stats["regional_rows"] == 3
    assert stats["regional_rows_with_successful_nonempty_text"] == 3
    assert stats["globally_unique_polygons"] == 3
    assert stats["unique_polygons_with_successful_nonempty_text"] == 3
    assert stats["regional_overlap_duplicate_rows"] == 0
    assert stats["regional_overlap_duplicate_rate"] == 0.0
    assert stats["manifest_duplicate_rows"] == 0
    assert stats["osm_types"] == {"relation": 1, "way": 2}
    assert stats["geometry_types"] == {"MultiPolygon": 1, "Polygon": 2}
    assert stats["description_suffixes"] == {"en": 2, "pt-BR": 1}
    assert stats["rejections"] == {"no_nonempty_description": 4}
    assert stats["text_rejection_counts"]["no_nonempty_description"] == 4
    assert stats["text_rejection_rows"] == 4
    assert stats["emitted_features"] == 7
    assert stats["base_description_rows"] == 0
    assert stats["localized_description_rows"] == 3
    assert "generation_timestamp_utc" not in stats
    assert stats["area_m2_total_m2"] > 0
    assert stats["area_m2_mean_m2"] == pytest.approx(stats["area_m2_total_m2"] / 3)
    assert stats["dataset_bbox"] == [0.0, 0.0, 11.0, 11.0]
    assert stats["geometry_vertices_total"] == 12
    assert stats["geometry_rings_total"] == 3
    assert stats["geometry_holes_total"] == 0
    assert stats["multipolygon_components_total"] == 1
    assert stats["area_m2_min_m2"] is not None and stats["area_m2_min_m2"] > 0
    assert stats["area_m2_max_m2"] >= stats["area_m2_min_m2"]
    assert stats["stats_schema_version"] == 8


def test_statistics_media_and_card_use_one_canonical_row_per_osm_identity(
    tmp_path: Path,
) -> None:
    data_root = tmp_path / "generated"
    source_root = tmp_path / "raw"
    source_root.mkdir()
    old_duplicate = make_record_dict(
        Polygon([(0, 0), (0, 1), (1, 1), (1, 0)]),
        {"description": "old duplicate"},
        osm_id=42,
        source_pbf="region-a.osm.pbf",
    )
    old_duplicate["version"] = 1
    old_duplicate["area_m2"] = 1.0
    canonical_duplicate = make_record_dict(
        Polygon([(10, 10), (10, 11), (11, 11), (11, 10)]),
        {"description": "canonical duplicate"},
        osm_id=42,
        source_pbf="region-b.osm.pbf",
    )
    canonical_duplicate["version"] = 2
    canonical_duplicate["area_m2"] = 1_000.0
    other = make_record_dict(
        Polygon([(20, 20), (20, 21), (21, 21), (21, 20)]),
        {"description": "other"},
        osm_id=42,
        osm_type="relation",
        source_pbf="region-b.osm.pbf",
    )
    other["area_m2"] = 10.0
    write_finalized_dataset(
        data_root,
        source_root,
        {"region-a": [old_duplicate], "region-b": [canonical_duplicate, other]},
    )

    stats = collect_stats(data_root, clock=_frozen_clock)
    h3_counts = aggregate_h3_density(data_root)
    area_counts = aggregate_area_histogram(data_root)
    generated = generate_dataset_docs(data_root, _TEMPLATE_PATH)
    card = (data_root / "README.md").read_text(encoding="utf-8")

    assert stats["rows"] == 2
    assert stats["unique_osm_objects"] == 2
    assert stats["regional_overlap_duplicate_rows"] == 1
    assert stats["regional_rows_with_successful_nonempty_text"] == 3
    assert stats["unique_polygons_with_successful_nonempty_text"] == 2
    assert stats["area_m2_population"] == (
        "unique_polygons_with_successfully_extracted_trimmed_nonempty_text"
    )
    assert stats["osm_types"] == {"relation": 1, "way": 1}
    assert stats["area_m2_count"] == 2
    assert stats["area_m2_total_m2"] == pytest.approx(1_010.0)
    assert stats["dataset_bbox"] == [10.0, 10.0, 21.0, 21.0]
    assert sum(h3_counts.values()) == 2
    assert sum(area_counts.values()) == 2
    assert area_counts["1-10 m²"] == 0
    assert area_counts["10-100 m²"] == 1
    assert area_counts["1k-10k m²"] == 1
    assert generated["rows"] == 2
    assert stats["regional_rows"] == 3
    assert stats["globally_unique_polygons"] == 2
    assert stats["manifest_duplicate_rows"] == 0
    assert "| Regional/raw polygon rows | 3 |" in card
    assert (
        "| Canonical globally unique `(osm_type, osm_id)` polygons with successfully "
        "extracted trimmed non-empty description text | 2 |"
    ) in card
    assert "| Regional-overlap duplicate rows | 1 |" in card
    assert "| Manifest duplicate rows rejected | 0 |" in card
    assert (
        "| Canonical globally unique `(osm_type, osm_id)` polygons with successfully "
        "extracted trimmed non-empty description text | 2 |"
    ) in card


def test_unique_row_selection_is_stable_when_equal_ranked_input_order_reverses(
    tmp_path: Path,
) -> None:
    first = make_record_dict(
        Polygon([(0, 0), (0, 1), (1, 1), (1, 0)]),
        {"description": "same"},
        osm_id=77,
        source_pbf="region.osm.pbf",
    )
    second = make_record_dict(
        Polygon([(10, 10), (10, 11), (11, 11), (11, 10)]),
        {"description": "same"},
        osm_id=77,
        source_pbf="region.osm.pbf",
    )
    second["version"] = int(first["version"]) + 1

    def selected_rows(name: str, records: list[dict[str, object]]) -> list[dict[str, object]]:
        data_root = tmp_path / name / "generated"
        source_root = tmp_path / name / "raw"
        write_finalized_dataset(
            data_root,
            source_root,
            {"region-a": [records[0]], "region-b": [records[1]]},
        )
        return [
            row
            for batch in iter_unique_parquet_batches(
                data_root,
                columns=(
                    "osm_type",
                    "osm_id",
                    "description",
                    "area_m2",
                    "bbox_min_x",
                    "bbox_min_y",
                    "bbox_max_x",
                    "bbox_max_y",
                    "geometry",
                ),
            )
            for row in batch.to_pylist()
        ]

    forward = selected_rows("forward", [first, second])
    reverse = selected_rows("reverse", [second, first])
    expected = select_canonical_row((first, second))

    assert forward == reverse
    assert forward[0]["osm_id"] == 77
    assert forward[0]["description"] == expected["description"]
    assert forward[0]["area_m2"] == pytest.approx(expected["area_m2"])
    assert (
        forward[0]["bbox_min_x"],
        forward[0]["bbox_min_y"],
        forward[0]["bbox_max_x"],
        forward[0]["bbox_max_y"],
    ) == (
        expected["bbox_min_x"],
        expected["bbox_min_y"],
        expected["bbox_max_x"],
        expected["bbox_max_y"],
    )


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

    assert stats["stats_schema_version"] == 8
    assert stats["base_description_values"] == 2
    assert stats["base_description_words_total"] == 3
    assert stats["base_description_words_median"] == 1.5
    assert stats["localized_description_values"] == 3
    assert stats["localized_description_words_total"] == 6
    assert stats["localized_description_words_median"] == 2.0


def test_collect_stats_counts_holes_and_multipolygon_parts_from_every_row(
    tmp_path: Path,
) -> None:
    data_root = tmp_path / "generated"
    source_root = tmp_path / "raw"
    source_root.mkdir()
    outer = [(0, 0), (0, 4), (4, 4), (4, 0), (0, 0)]
    hole = [(1, 1), (1, 2), (2, 2), (2, 1), (1, 1)]
    multipolygon = MultiPolygon(
        [
            Polygon([(10, 10), (10, 11), (11, 11), (11, 10)]),
            Polygon([(20, 20), (20, 21), (21, 21), (21, 20)]),
        ]
    )
    write_finalized_dataset(
        data_root,
        source_root,
        {
            "complex": [
                make_record_dict(
                    Polygon(outer, [hole]),
                    {"description": "with hole"},
                    osm_id=1,
                    source_pbf="complex.osm.pbf",
                ),
                make_record_dict(
                    multipolygon,
                    {"description": "two parts"},
                    osm_type="relation",
                    osm_id=2,
                    source_pbf="complex.osm.pbf",
                ),
            ]
        },
    )

    stats = collect_stats(data_root)

    assert stats["geometry_vertices_total"] == 16
    assert stats["geometry_rings_total"] == 4
    assert stats["geometry_holes_total"] == 1
    assert stats["multipolygon_components_total"] == 2
    assert stats["dataset_bbox"] == [0.0, 0.0, 21.0, 21.0]


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


def test_collect_stats_rejects_final_artifact_without_successful_text(
    tmp_path: Path,
) -> None:
    data_root = tmp_path / "generated"
    source_root = tmp_path / "raw"
    source_root.mkdir()
    invalid = make_record_dict(
        Polygon([(0, 0), (0, 1), (1, 1), (1, 0)]),
        {"description": "temporary text"},
        osm_id=91,
        source_pbf="invalid.osm.pbf",
    )
    invalid["description"] = "   "
    invalid["localized_descriptions"] = {}

    # Build an intentionally invalid fixture without changing the production
    # writer's normal validation path.
    with patch(
        "osm_polygon_description_tag.dataset.storage.validate_geoparquet",
        return_value=1,
    ):
        write_finalized_dataset(data_root, source_root, {"invalid": [invalid]})

    with pytest.raises(StorageError, match="non-empty"):
        collect_stats(data_root, clock=_frozen_clock)


def test_generate_dataset_docs_installs_hero_image(tmp_path: Path) -> None:
    data_root = tmp_path / "generated"
    source_root = tmp_path / "raw"
    source_root.mkdir()
    write_reporting_fixture(data_root, source_root)
    template_path = _TEMPLATE_PATH

    generate_dataset_docs(data_root, template_path, clock=_frozen_clock)

    hero = data_root / "assets" / "dataset-card-hero.png"
    assert hero.read_bytes() == _repository_file("assets/dataset-card-hero.png").read_bytes()
    first_mtime = hero.stat().st_mtime_ns

    readme = (data_root / "README.md").read_text(encoding="utf-8")
    assert "![OSM Polygon Description Tag dataset hero](assets/dataset-card-hero.png)" in readme

    # Re-running with identical inputs leaves the hero byte-identical and
    # preserves its on-disk mtime.
    generate_dataset_docs(data_root, template_path, clock=_frozen_clock)
    assert hero.read_bytes() == _repository_file("assets/dataset-card-hero.png").read_bytes()
    assert hero.stat().st_mtime_ns == first_mtime


def test_generate_dataset_docs_writes_stats_and_card(tmp_path: Path) -> None:
    data_root = tmp_path / "generated"
    source_root = tmp_path / "raw"
    source_root.mkdir()
    write_reporting_fixture(data_root, source_root)
    template_path = _TEMPLATE_PATH

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
