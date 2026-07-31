"""RED tests proving the resumability contract ignores cosmetic code revisions.

A documentation-only commit changes the project HEAD but must NOT invalidate
existing Parquet outputs. Similarly, rebuilding the package without
modifying the output-producing algorithm must not force a rebuild.

The behavioral contract uses:

- ``transform_algorithm_version``
- ``schema_version``
- ``geoparquet_version``
- ``area_policy_sha256``
- ``source`` identity
- ``output`` identity
- ``output_algorithm_revision``

``code_revision`` is provenance only.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from shapely.geometry import Polygon

from osm_polygon_description_tag.dataset.manifest import (
    Manifest,
    RunCounts,
    current_area_policy_sha256,
    current_output_algorithm_revision,
    is_resumable,
    output_identity_for,
    source_identity_for,
)


def test_is_resumable_with_mismatched_code_revision(tmp_path: Path) -> None:
    """A documentation-only commit (changed code_revision) must NOT invalidate resume."""
    source = tmp_path / "a.osm.pbf"
    output = tmp_path / "a.parquet"
    source.write_bytes(b"a")
    output.write_bytes(b"out")

    src_identity = source_identity_for(source)
    out_identity = output_identity_for(output)

    manifest = Manifest(
        manifest_schema_version=2,
        schema_version=2,
        geoparquet_version="1.1.0",
        transform_algorithm_version=2,
        area_policy_sha256=current_area_policy_sha256(),
        output_algorithm_revision=current_output_algorithm_revision(),
        source=src_identity,
        output=out_identity,
        osmium_version="osmium version 1.19.1",
        dependency_versions={"pyarrow": "20.0.0"},
        code_revision="STALE-DOC-ONLY-COMMIT",
        started_at="2026-07-27T00:00:00+00:00",
        completed_at="2026-07-27T00:01:00+00:00",
        counts=RunCounts(emitted_features=1, included_rows=1, rejections={}),
    )
    assert is_resumable(manifest, src_identity, out_identity) is True, (
        "doc-only commit must not invalidate valid artifacts"
    )


def test_is_resumable_with_missing_code_revision(tmp_path: Path) -> None:
    """A manifest whose code_revision is None (env didn't capture it) must still be resumable."""
    source = tmp_path / "a.osm.pbf"
    output = tmp_path / "a.parquet"
    source.write_bytes(b"a")
    output.write_bytes(b"out")

    manifest = Manifest(
        manifest_schema_version=2,
        schema_version=2,
        geoparquet_version="1.1.0",
        transform_algorithm_version=2,
        area_policy_sha256=current_area_policy_sha256(),
        output_algorithm_revision=current_output_algorithm_revision(),
        source=source_identity_for(source),
        output=output_identity_for(output),
        osmium_version="osmium version 1.19.1",
        dependency_versions={"pyarrow": "20.0.0"},
        code_revision=None,
        started_at="2026-07-27T00:00:00+00:00",
        completed_at="2026-07-27T00:01:00+00:00",
        counts=RunCounts(emitted_features=1, included_rows=1, rejections={}),
    )
    assert is_resumable(manifest, source_identity_for(source), output_identity_for(output)) is True


def test_behavioral_change_invalidates_resume(tmp_path: Path) -> None:
    """A change to ``output_algorithm_revision`` must invalidate resume."""
    source = tmp_path / "a.osm.pbf"
    output = tmp_path / "a.parquet"
    source.write_bytes(b"a")
    output.write_bytes(b"out")
    manifest = Manifest(
        manifest_schema_version=2,
        schema_version=2,
        geoparquet_version="1.1.0",
        transform_algorithm_version=2,
        area_policy_sha256=current_area_policy_sha256(),
        output_algorithm_revision="old:0000",
        source=source_identity_for(source),
        output=output_identity_for(output),
        osmium_version="osmium version 1.19.1",
        dependency_versions={"pyarrow": "20.0.0"},
        code_revision=None,
        started_at="2026-07-27T00:00:00+00:00",
        completed_at="2026-07-27T00:01:00+00:00",
        counts=RunCounts(emitted_features=1, included_rows=1, rejections={}),
    )
    assert is_resumable(manifest, source_identity_for(source), output_identity_for(output)) is False


def test_full_run_no_rebuild_on_doc_only_commit(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """When the orchestrator runs against a previously published PBF, no upload must occur."""
    from osm_polygon_description_tag.config import Paths
    from osm_polygon_description_tag.orchestrator import (
        PUBLICATION_STATE_FILENAME,
        run_and_publish,
    )

    source_root = tmp_path / "raw"
    data_root = tmp_path / "generated"
    source_root.mkdir()
    data_root.mkdir()
    (source_root / "a.osm.pbf").write_bytes(b"a-bytes")
    paths = Paths(source_root=source_root, data_root=data_root)

    from osm_polygon_description_tag.dataset.storage import write_geoparquet
    from tests.conftest import make_record_dict

    write_geoparquet(
        iter(
            [
                make_record_dict(
                    Polygon([(0, 0), (0, 1), (1, 1), (1, 0)]),
                    {"description": "x"},
                    osm_id=1,
                    source_pbf="a.osm.pbf",
                )
            ]
        ),
        paths.data_root / "data" / "a.parquet",
        batch_size=10,
    )
    (paths.data_root / "data").mkdir(parents=True, exist_ok=True)
    (paths.data_root / "manifests").mkdir(parents=True, exist_ok=True)

    from osm_polygon_description_tag.dataset.manifest import write_manifest

    write_manifest(
        Manifest(
            manifest_schema_version=2,
            schema_version=2,
            geoparquet_version="1.1.0",
            transform_algorithm_version=2,
            area_policy_sha256=current_area_policy_sha256(),
            output_algorithm_revision=current_output_algorithm_revision(),
            source=source_identity_for(source_root / "a.osm.pbf"),
            output=output_identity_for(paths.data_root / "data" / "a.parquet"),
            osmium_version="osmium version 1.19.1",
            dependency_versions={"pyarrow": "20.0.0"},
            code_revision="DOC-ONLY-COMMIT",
            started_at="2026-07-27T00:00:00+00:00",
            completed_at="2026-07-27T00:01:00+00:00",
            counts=RunCounts(emitted_features=1, included_rows=1, rejections={}),
        ),
        paths.data_root / "manifests" / "a.manifest.json",
    )
    # Defensive double-write for tests that follow before this edit.

    # Plant the canonical card that ``generate_dataset_docs`` would
    # produce so the metadata identity remains stable after the
    # orchestrator's refresh step.
    from osm_polygon_description_tag._resources import dataset_card_template
    from osm_polygon_description_tag.dataset.reporting import generate_dataset_docs

    generate_dataset_docs(
        paths.data_root,
        dataset_card_template(),
        clock=lambda: "2026-07-27T00:00:00+00:00",
    )
    # ``generate_dataset_docs`` already wrote the README, stats.json,
    # and assets/description_polygon_density.png atomically. The
    # doc-only-commit test asserts zero uploads on a fully-published
    # workspace whose regenerated artifacts match the recorded state.
    assert (paths.data_root / "assets" / "description_polygon_density.png").is_file()

    # Mark a.osm.pbf as already published AND mark the metadata as
    # already published so the fully-completed run is a no-op.
    from osm_polygon_description_tag.dataset.manifest import file_sha256
    from osm_polygon_description_tag.publication import _build_metadata_only_upload_plan

    metadata_plan = _build_metadata_only_upload_plan(paths.data_root)
    state = {
        "schema_version": 1,
        "published": {
            "a.osm.pbf": {
                "source_sha256": source_identity_for(source_root / "a.osm.pbf").sha256,
                "output_sha256": output_identity_for(paths.data_root / "data" / "a.parquet").sha256,
                "output_bytes": (paths.data_root / "data" / "a.parquet").stat().st_size,
                "remote_revision": "rev-a",
                "artifact_identity": "00" * 32,
                "completed_at": "2026-07-27T00:00:00+00:00",
            }
        },
        "metadata": {
            "identity_sha256": metadata_plan.identity_sha256,
            "readme_sha256": file_sha256(paths.data_root / "README.md"),
            "stats_sha256": file_sha256(paths.data_root / "stats.json"),
            "readme_size_bytes": (paths.data_root / "README.md").stat().st_size,
            "stats_size_bytes": (paths.data_root / "stats.json").stat().st_size,
            "h3_map_sha256": file_sha256(
                paths.data_root / "assets" / "description_polygon_density.png"
            ),
            "h3_map_size_bytes": (paths.data_root / "assets" / "description_polygon_density.png")
            .stat()
            .st_size,
            "area_histogram_sha256": file_sha256(
                paths.data_root / "assets" / "area_distribution.png"
            ),
            "area_histogram_size_bytes": (paths.data_root / "assets" / "area_distribution.png")
            .stat()
            .st_size,
            "verified_revision": "rev-meta",
            "completed_at": "2026-07-27T00:00:00+00:00",
        },
    }
    (paths.data_root / PUBLICATION_STATE_FILENAME).write_text(
        json.dumps(state, sort_keys=True, indent=2), encoding="utf-8"
    )
    (paths.data_root / "README.md").write_text("stub")
    (paths.data_root / "stats.json").write_text("{}")

    seen_calls: list[list[str]] = []

    def upload_runner(command: list[str]) -> str:
        seen_calls.append(list(command))
        return "rev-a"

    report = run_and_publish(
        paths=paths,
        confirm_repo="NoeFlandre/osm-polygon-description-tag",
        preflight=lambda: {"preflight": "stub", "source_count": 1},
        upload_runner=upload_runner,
        clock=lambda: "2026-07-27T00:00:00+00:00",
        exporter=object(),  # not reached
        verifier=lambda repo_id, files: "rev-a",
    )

    # Per-PBF outcome must be 'already-published'. Zero uploads must have occurred.
    assert report.outcomes[0].status == "already-published"
    assert seen_calls == [], "no upload should occur on a doc-only/commit undisturbed run"
