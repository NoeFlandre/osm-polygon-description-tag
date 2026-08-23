"""Tests for the shared publication metadata-artifact contract."""

from osm_polygon_description_tag.publication import planning
from osm_polygon_description_tag.publication.artifacts import METADATA_ARTIFACTS


def test_metadata_artifact_registry_preserves_publication_identity_contract() -> None:
    assert planning.METADATA_ARTIFACTS is METADATA_ARTIFACTS

    assert [
        (artifact.key, artifact.relative_path, artifact.sha256_field, artifact.size_field)
        for artifact in METADATA_ARTIFACTS
    ] == [
        ("readme", "README.md", "readme_sha256", "readme_size_bytes"),
        ("stats", "stats.json", "stats_sha256", "stats_size_bytes"),
        (
            "h3_map",
            "assets/description_polygon_density.png",
            "h3_map_sha256",
            "h3_map_size_bytes",
        ),
        (
            "area_histogram",
            "assets/area_distribution.png",
            "area_histogram_sha256",
            "area_histogram_size_bytes",
        ),
        (
            "hero",
            "assets/dataset-card-hero.png",
            "dataset_card_hero_sha256",
            "dataset_card_hero_size_bytes",
        ),
    ]
