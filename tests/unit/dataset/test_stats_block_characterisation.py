"""Characterisation of the generated dataset-card statistics block.

The digests were recorded before the statistics-section split. They pin the
exact bytes of ``render_stats_block`` for each branch the split moves, so any
change in what the card shows fails here rather than in a published artifact.
"""

from __future__ import annotations

import hashlib
from typing import Any

import pytest

from osm_polygon_description_tag.dataset.stats_card import render_stats_block

_STATS_SHA256 = "c" * 64


def _full_stats() -> dict[str, Any]:
    return {
        "stats_schema_version": 3,
        "schema_version": 4,
        "rows": 120,
        "output_files": 3,
        "output_bytes_total": 7340032,
        "globally_unique_polygons": 100,
        "regional_overlap_duplicate_rows": 20,
        "regional_rows": 120,
        "manifest_duplicate_rows": 5,
        "unique_polygons_with_successful_nonempty_text": 90,
        "osm_types": {"way": 60, "relation": 40},
        "geometry_types": {"Polygon": 70, "MultiPolygon": 30},
        "base_description_values": 1500,
        "base_description_words_total": 9000,
        "base_description_words_median": 6.0,
        "localized_description_values": 800,
        "localized_description_words_total": 4321.5,
        "localized_description_words_median": None,
        "description_suffixes": {"fr": 12, "de": 12, "en": 3, "xx-odd": 1},
        "text_rejection_counts": {"empty": 4, "bad": 2},
        "persisted_text_rejection_rows": 7,
        "data_min_timestamp_utc": "2010-01-01T00:00:00Z",
        "data_max_timestamp_utc": "2025-06-30T23:59:59Z",
        "area_m2_total_m2": 1234567890.5,
        "area_m2_mean_m2": 12345.678,
        "area_m2_min_m2": 0.25,
        "area_m2_max_m2": 2.5e9,
        "area_m2_p25_m2": 0.9,
        "area_m2_median_m2": 42.0,
        "area_m2_p75_m2": 1000.0,
        "dataset_bbox": [-4.5, 47.25, 2.75, 49.0],
        "geometry_vertices_total": 123456,
        "geometry_rings_total": 1000,
        "geometry_holes_total": 12,
        "multipolygon_components_total": 77,
    }


def _minimal_stats() -> dict[str, Any]:
    return {
        "stats_schema_version": 3,
        "schema_version": 4,
        "rows": 0,
        "output_files": 0,
        "output_bytes_total": 0,
        "osm_types": {},
        "geometry_types": {},
        "base_description_values": 0,
        "base_description_words_total": 0,
        "base_description_words_median": None,
        "localized_description_values": 0,
        "localized_description_words_total": 0,
        "localized_description_words_median": None,
        "description_suffixes": {},
        "text_rejection_counts": None,
        "data_min_timestamp_utc": None,
        "data_max_timestamp_utc": None,
        "area_m2_total_m2": None,
        "area_m2_mean_m2": None,
        "dataset_bbox": None,
    }


def _edge_stats() -> dict[str, Any]:
    stats = _full_stats()
    stats.update(
        {
            "output_bytes_total": 3 * 1024**4,
            "text_rejection_counts": "not-a-mapping",
            "persisted_text_rejection_rows": 0,
            "dataset_bbox": [float("inf"), 0.0, 1.0, 1.0],
            "area_m2_total_m2": float("nan"),
            "area_m2_mean_m2": float("inf"),
            "area_m2_median_m2": 3.0,
        }
    )
    return stats


_RECORDED_DIGESTS = {
    "full": "6ea9bb36b9495187c1b04ab768b2e2792db67d32a97a89c70dc14ed4926f26e2",
    "minimal": "1cb904e7148f47144037e7c442c2a0c8310151e64062f6441df41016e533c091",
    "edge": "06dfe410d573ddf341053343cf00ab2f39e16f1d2d11f785af501043ab82909d",
}
_VARIANTS = {"full": _full_stats, "minimal": _minimal_stats, "edge": _edge_stats}


@pytest.mark.parametrize("variant", sorted(_VARIANTS))
def test_stats_block_bytes_match_the_recorded_digest(variant: str) -> None:
    rendered = render_stats_block(_VARIANTS[variant](), _STATS_SHA256)

    assert hashlib.sha256(rendered.encode("utf-8")).hexdigest() == _RECORDED_DIGESTS[variant]


def test_stats_block_leads_with_the_recorded_identity_comments() -> None:
    rendered = render_stats_block(_full_stats(), _STATS_SHA256)

    assert rendered.splitlines()[:3] == [
        f"<!-- stats_sha256: {_STATS_SHA256} -->",
        "<!-- stats_schema_version: 3 -->",
        "<!-- schema_version: 4 -->",
    ]
