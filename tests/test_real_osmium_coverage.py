"""Real-osmium coverage contract for the amendment dataset.

The amendment closes the incomplete closed-way area_tags list by relying on
osmium's documented general area handling (``area_tags: true``,
``linear_tags: true``) and the ``--geometry-types polygon`` selection. The
executable ``osmium`` is required to be installed on PATH; these tests
convert a synthetic XML fixture to PBF in a temporary directory, run the
real osmium binary, and assert the exact final Parquet IDs and tag
fidelity.

Failure modes that must be exercised:

- closed way with description only is included;
- closed way with name and description is included;
- closed shop, tourism, place, power polygons are included;
- closed way with area=yes is included;
- closed way with area=no is excluded;
- open way is excluded;
- multipolygon relation is included;
- boundary relation is included;
- node with description is excluded;
- undescribed way or relation is excluded.

The tests skip if the real osmium binary is unavailable.
"""

from __future__ import annotations

import json
import shutil
import subprocess
from collections.abc import Iterator
from pathlib import Path

import pyarrow.parquet as pq
import pytest
from shapely import to_wkb
from shapely.geometry import Polygon

from osm_polygon_description_tag._resources import (
    dataset_card_template,
    osmium_export_config,
)
from osm_polygon_description_tag.config import Paths
from osm_polygon_description_tag.discovery import discover_sources
from osm_polygon_description_tag.manifest import (
    Manifest,
    RunCounts,
    current_area_policy_sha256,
    current_output_algorithm_revision,
    output_identity_for,
    source_identity_for,
    write_manifest,
)
from osm_polygon_description_tag.pipeline import build_one
from osm_polygon_description_tag.reporting import generate_dataset_docs
from osm_polygon_description_tag.storage import write_geoparquet

FIXTURE = Path("tests/fixtures/amendment_coverage.osm")
EXPECTED_INCLUDED = {1100, 1101, 1102, 1103, 1104, 1105, 1106, 1300, 1500}
EXPECTED_EXCLUDED = {1107, 1108, 1109, 1600, 8001}


@pytest.fixture
def _real_osmium() -> Iterator[str]:
    executable = shutil.which("osmium")
    if executable is None:
        pytest.skip("osmium binary not installed")
    yield executable


def _write_pbf(executable: str, osm_path: Path, pbf_path: Path) -> None:
    completed = subprocess.run(  # noqa: S603 - controlled argument array, no shell
        [executable, "cat", str(osm_path), "-o", str(pbf_path), "--overwrite"],
        check=True,
        capture_output=True,
        shell=False,
        timeout=30,
    )
    assert completed.returncode == 0, completed.stderr.decode("utf-8", errors="replace")


def _build_dataset(tmp_path: Path, executable: str) -> tuple[Paths, Path, Path]:
    workspace = tmp_path / "amendment"
    source_root = workspace / "raw"
    data_root = workspace / "generated"
    source_root.mkdir(parents=True)
    data_root.mkdir(parents=True)
    pbf_path = source_root / "amendment.osm.pbf"
    _write_pbf(executable, FIXTURE, pbf_path)
    paths = Paths(source_root=source_root, data_root=data_root)
    return paths, source_root, data_root


def _plant_minimal_artifact(data_root: Path, source_root: Path) -> None:
    """Plant a minimal README/stats so the per-PBF plan and reporting are valid."""
    (data_root / "data").mkdir(parents=True, exist_ok=True)
    (data_root / "manifests").mkdir(parents=True, exist_ok=True)
    write_geoparquet(
        iter(
            [
                {
                    "source_pbf": "amendment.osm.pbf",
                    "osm_type": "way",
                    "osm_id": 1,
                    "osm_url": "https://www.openstreetmap.org/way/1",
                    "version": 1,
                    "changeset": 1,
                    "timestamp": None,
                    "name": "Seed",
                    "localized_names": {},
                    "description": "Seed",
                    "localized_descriptions": {},
                    "tags": {"description": "Seed"},
                    "geometry_type": "Polygon",
                    "area_m2": 1.0,
                    "bbox_min_x": 0.0,
                    "bbox_min_y": 0.0,
                    "bbox_max_x": 0.0,
                    "bbox_max_y": 0.0,
                    "geometry": to_wkb(
                        Polygon([(0, 0), (0, 1), (1, 1), (1, 0)]), output_dimension=2
                    ),
                }
            ]
        ),
        data_root / "data" / "seed.parquet",
        batch_size=10,
    )
    write_manifest(
        Manifest(
            manifest_schema_version=2,
            schema_version=2,
            geoparquet_version="1.1.0",
            transform_algorithm_version=2,
            area_policy_sha256=current_area_policy_sha256(),
            output_algorithm_revision=current_output_algorithm_revision(),
            source=source_identity_for(source_root / "seed.osm.pbf"),
            output=output_identity_for(data_root / "data" / "seed.parquet"),
            osmium_version=None,
            dependency_versions={},
            code_revision=None,
            started_at="2026-01-01T00:00:00+00:00",
            completed_at="2026-01-01T00:00:01+00:00",
            counts=RunCounts(emitted_features=1, included_rows=1, rejections={}),
        ),
        data_root / "manifests" / "seed.manifest.json",
    )


def test_real_osmium_emits_amendment_inclusion_set(tmp_path: Path, _real_osmium: str) -> None:
    """A real osmium run produces exactly the included IDs and excludes the rest."""
    paths, source_root, data_root = _build_dataset(tmp_path, _real_osmium)
    sources = discover_sources(source_root)
    assert [source.name for source in sources] == ["amendment.osm.pbf"]

    (data_root / "data").mkdir(parents=True, exist_ok=True)
    (data_root / "manifests").mkdir(parents=True, exist_ok=True)

    result = build_one(
        sources[0],
        paths,
        export_config=osmium_export_config(),
        executable="osmium",
    )

    assert result.status == "built", result.status
    assert result.included_rows == len(EXPECTED_INCLUDED)
    table = pq.read_table(result.output_path)
    osm_type = table.column("osm_type").to_pylist()
    osm_ids = set(table.column("osm_id").to_pylist())
    assert all(value in {"way", "relation"} for value in osm_type)
    assert osm_ids == EXPECTED_INCLUDED, f"unexpected IDs {osm_ids}"
    for excluded in EXPECTED_EXCLUDED:
        assert excluded not in osm_ids, f"excluded ID present: {excluded}"


def test_included_records_preserve_original_tags_exactly(tmp_path: Path, _real_osmium: str) -> None:
    """Every included record's ``tags`` map contains the original keys verbatim."""
    paths, _source_root, data_root = _build_dataset(tmp_path, _real_osmium)
    source = discover_sources(paths.source_root)[0]
    (data_root / "data").mkdir(parents=True, exist_ok=True)
    (data_root / "manifests").mkdir(parents=True, exist_ok=True)

    result = build_one(
        source,
        paths,
        export_config=osmium_export_config(),
        executable="osmium",
    )

    table = pq.read_table(result.output_path)
    by_id = {
        (row_type, row_id): tags
        for row_type, row_id, tags in zip(
            table.column("osm_type").to_pylist(),
            table.column("osm_id").to_pylist(),
            table.column("tags").to_pylist(),
            strict=True,
        )
    }

    expected_tags = {
        ("way", 1100): {"description": "Closed way with description only"},
        ("way", 1101): {
            "name": "Polygons Building",
            "description": "Name plus description",
            "name:pt-BR": "Prédio dos Polígonos",
        },
        ("way", 1102): {
            "shop": "bakery",
            "name": "Bakery",
            "description": "Closed shop polygon",
        },
        ("way", 1106): {
            "highway": "pedestrian",
            "area": "yes",
            "name": "Square",
            "description": "area=yes pedestrian square",
        },
        ("relation", 1300): {
            "name": "Lake with island",
            "description": "Multipolygon lake",
            "description:en": "Lake with island",
        },
        ("relation", 1500): {
            "boundary": "protected_area",
            "description": "Protected boundary area",
        },
    }
    for key, expected in expected_tags.items():
        actual = dict(by_id[key])
        assert actual == expected, f"tags mismatch for {key}: {actual} != {expected}"


def test_real_osmium_area_no_way_is_excluded(tmp_path: Path, _real_osmium: str) -> None:
    """The closed way with ``area=no`` is never emitted as a polygon."""
    paths, _source_root, data_root = _build_dataset(tmp_path, _real_osmium)
    source = discover_sources(paths.source_root)[0]
    (data_root / "data").mkdir(parents=True, exist_ok=True)
    (data_root / "manifests").mkdir(parents=True, exist_ok=True)

    result = build_one(
        source,
        paths,
        export_config=osmium_export_config(),
        executable="osmium",
    )

    table = pq.read_table(result.output_path)
    assert 1107 not in set(table.column("osm_id").to_pylist())


def test_stats_block_aggregates_name_suffixes(tmp_path: Path, _real_osmium: str) -> None:
    """The deterministic stats block reports exact name/description suffix counts."""
    paths, _source_root, data_root = _build_dataset(tmp_path, _real_osmium)
    source = discover_sources(paths.source_root)[0]
    (data_root / "data").mkdir(parents=True, exist_ok=True)
    (data_root / "manifests").mkdir(parents=True, exist_ok=True)

    build_one(
        source,
        paths,
        export_config=osmium_export_config(),
        executable="osmium",
    )
    stats = generate_dataset_docs(paths.data_root, dataset_card_template())
    assert stats["name_suffixes"] == {"pt-BR": 1}
    assert stats["base_name_rows"] == 7
    assert stats["localized_name_rows"] == 1
    payload = json.loads((data_root / "stats.json").read_text(encoding="utf-8"))
    assert payload["name_suffixes"] == {"pt-BR": 1}
    assert payload["base_name_rows"] == 7
