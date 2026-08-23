"""Shared definitions for publication metadata artifacts."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True, slots=True)
class MetadataArtifact:
    """Describe one metadata file and its publication-state identity fields."""

    key: str
    relative_path: str
    sha256_field: str
    size_field: str

    @property
    def filename(self) -> str:
        return self.relative_path.rsplit("/", maxsplit=1)[-1]


README_ARTIFACT = MetadataArtifact(
    key="readme",
    relative_path="README.md",
    sha256_field="readme_sha256",
    size_field="readme_size_bytes",
)
STATS_ARTIFACT = MetadataArtifact(
    key="stats",
    relative_path="stats.json",
    sha256_field="stats_sha256",
    size_field="stats_size_bytes",
)
H3_MAP_ARTIFACT = MetadataArtifact(
    key="h3_map",
    relative_path="assets/description_polygon_density.png",
    sha256_field="h3_map_sha256",
    size_field="h3_map_size_bytes",
)
AREA_HISTOGRAM_ARTIFACT = MetadataArtifact(
    key="area_histogram",
    relative_path="assets/area_distribution.png",
    sha256_field="area_histogram_sha256",
    size_field="area_histogram_size_bytes",
)
DATASET_CARD_HERO_ARTIFACT = MetadataArtifact(
    key="hero",
    relative_path="assets/dataset-card-hero.png",
    sha256_field="dataset_card_hero_sha256",
    size_field="dataset_card_hero_size_bytes",
)

DOCUMENT_ARTIFACTS = (README_ARTIFACT, STATS_ARTIFACT)
CORE_ASSET_ARTIFACTS = (H3_MAP_ARTIFACT, AREA_HISTOGRAM_ARTIFACT)
VISUAL_ARTIFACTS = (*CORE_ASSET_ARTIFACTS, DATASET_CARD_HERO_ARTIFACT)
METADATA_ARTIFACTS = (*DOCUMENT_ARTIFACTS, *VISUAL_ARTIFACTS)


def metadata_paths(data_root: Path) -> dict[str, Path]:
    """Return the managed metadata paths keyed by their stable artifact keys."""
    return {artifact.key: data_root / artifact.relative_path for artifact in METADATA_ARTIFACTS}


__all__ = [
    "AREA_HISTOGRAM_ARTIFACT",
    "CORE_ASSET_ARTIFACTS",
    "DATASET_CARD_HERO_ARTIFACT",
    "DOCUMENT_ARTIFACTS",
    "H3_MAP_ARTIFACT",
    "METADATA_ARTIFACTS",
    "README_ARTIFACT",
    "STATS_ARTIFACT",
    "VISUAL_ARTIFACTS",
    "MetadataArtifact",
    "metadata_paths",
]
