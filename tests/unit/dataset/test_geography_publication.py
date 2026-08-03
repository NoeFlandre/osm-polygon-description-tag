"""Publication plan and state contract tests for the H3 density map.

These tests pin the publication integration:

* the per-PBF plan contains exactly five files including the map;
* the metadata-only plan contains exactly three files including the map;
* the map SHA-256 and size appear in the plan identity;
* a missing map fails plan construction before any upload runs;
* hidden, temp, and unrelated assets are rejected;
* maps participate in the publication-state no-op detection;
* an unchanged map preserves the true no-op metadata path;
* a changed map forces a fresh metadata upload.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from shapely.geometry import Polygon

from osm_polygon_description_tag._resources import project_code_revision
from osm_polygon_description_tag.config import Paths
from osm_polygon_description_tag.dataset.manifest import (
    Manifest,
    RunCounts,
    current_area_policy_sha256,
    current_output_algorithm_revision,
    file_sha256,
    output_identity_for,
    source_identity_for,
    write_manifest,
)
from osm_polygon_description_tag.dataset.storage import write_geoparquet
from osm_polygon_description_tag.orchestrator import PUBLICATION_STATE_FILENAME
from osm_polygon_description_tag.publication import (
    REPO_ID,
    PublicationError,
    _build_metadata_only_upload_plan,
    _build_per_pbf_upload_plan,
    create_upload_plan,
    per_pbf_command,
)
from osm_polygon_description_tag.publication.state import (
    _H3_MAP_SHA256_FIELD,
    _H3_MAP_SIZE_FIELD,
    H3_MAP_ASSET_RELATIVE_PATH,
    _metadata_state_matches,
)
from tests.conftest import make_record_dict

MAP_BYTES_A = b"\x89PNG\r\n\x1a\n" + b"A" * 4096
MAP_BYTES_B = b"\x89PNG\r\n\x1a\n" + b"B" * 4096


def _setup_two_sources(tmp_path: Path) -> tuple[Paths, Path, Path]:
    source_root = tmp_path / "raw"
    data_root = tmp_path / "generated"
    source_root.mkdir()
    data_root.mkdir()
    (source_root / "a.osm.pbf").write_bytes(b"a-bytes")
    (source_root / "b.osm.pbf").write_bytes(b"b-bytes")
    return Paths(source_root=source_root, data_root=data_root), source_root, data_root


def _plant_resumable_artifact(paths: Paths, source_root: Path, source_name: str) -> None:
    stem = source_name.removesuffix(".osm.pbf")
    (paths.data_root / "data").mkdir(parents=True, exist_ok=True)
    (paths.data_root / "manifests").mkdir(parents=True, exist_ok=True)
    write_geoparquet(
        iter(
            [
                make_record_dict(
                    Polygon([(0, 0), (0, 1), (1, 1), (1, 0)]),
                    {"description": "x"},
                    osm_id=1,
                    source_pbf=source_name,
                )
            ]
        ),
        paths.data_root / "data" / f"{stem}.parquet",
        batch_size=10,
    )
    write_manifest(
        Manifest(
            manifest_schema_version=2,
            schema_version=3,
            geoparquet_version="1.1.0",
            transform_algorithm_version=3,
            area_policy_sha256=current_area_policy_sha256(),
            output_algorithm_revision=current_output_algorithm_revision(),
            source=source_identity_for(source_root / source_name),
            output=output_identity_for(paths.data_root / "data" / f"{stem}.parquet"),
            osmium_version="osmium version 1.19.1",
            dependency_versions={"pyarrow": "20.0.0"},
            code_revision=project_code_revision(),
            started_at="2026-07-27T00:00:00+00:00",
            completed_at="2026-07-27T00:01:00+00:00",
            counts=RunCounts(emitted_features=1, included_rows=1, rejections={}),
        ),
        paths.data_root / "manifests" / f"{stem}.manifest.json",
    )


def _plant_metadata(paths: Paths) -> None:
    (paths.data_root / "README.md").write_text("# README")
    (paths.data_root / "stats.json").write_text("{}")
    (paths.data_root / "assets").mkdir(exist_ok=True)
    (paths.data_root / "assets" / "description_polygon_density.png").write_bytes(MAP_BYTES_A)
    (paths.data_root / "assets" / "area_distribution.png").write_bytes(MAP_BYTES_A)
    (paths.data_root / "assets" / "dataset-card-hero.png").write_bytes(MAP_BYTES_A)


# ---------------------------------------------------------------------------
# Per-PBF plan
# ---------------------------------------------------------------------------


def test_per_pbf_plan_contains_exactly_five_items_including_map(tmp_path: Path) -> None:
    paths, source_root, _data_root = _setup_two_sources(tmp_path)
    (source_root / "b.osm.pbf").unlink()
    _plant_resumable_artifact(paths, source_root, "a.osm.pbf")
    _plant_metadata(paths)

    plan = _build_per_pbf_upload_plan(paths.data_root, "a.osm.pbf")
    relative = sorted(item.relative_path for item in plan.files)
    assert relative == sorted(
        [
            "data/a.parquet",
            "manifests/a.manifest.json",
            "README.md",
            "stats.json",
            "assets/description_polygon_density.png",
            "assets/area_distribution.png",
            "assets/dataset-card-hero.png",
        ]
    )


def test_per_pbf_plan_fails_when_map_missing(tmp_path: Path) -> None:
    paths, source_root, _data_root = _setup_two_sources(tmp_path)
    (source_root / "b.osm.pbf").unlink()
    _plant_resumable_artifact(paths, source_root, "a.osm.pbf")
    (paths.data_root / "README.md").write_text("# README")
    (paths.data_root / "stats.json").write_text("{}")
    # No assets/ directory.

    with pytest.raises(PublicationError):
        _build_per_pbf_upload_plan(paths.data_root, "a.osm.pbf")


def test_per_pbf_command_lists_all_five_files(tmp_path: Path) -> None:
    paths, source_root, _data_root = _setup_two_sources(tmp_path)
    (source_root / "b.osm.pbf").unlink()
    _plant_resumable_artifact(paths, source_root, "a.osm.pbf")
    _plant_metadata(paths)
    cmd = per_pbf_command(paths.data_root, "a.osm.pbf")
    includes = sorted(cmd[index + 1] for index, piece in enumerate(cmd) if piece == "--include")
    assert includes == sorted(
        [
            "data/a.parquet",
            "manifests/a.manifest.json",
            "README.md",
            "stats.json",
            "assets/description_polygon_density.png",
            "assets/area_distribution.png",
            "assets/dataset-card-hero.png",
        ]
    )


# ---------------------------------------------------------------------------
# Metadata plan
# ---------------------------------------------------------------------------


def test_metadata_plan_contains_exactly_three_items_including_map(tmp_path: Path) -> None:
    paths, source_root, _data_root = _setup_two_sources(tmp_path)
    _plant_metadata(paths)
    plan = _build_metadata_only_upload_plan(paths.data_root)
    relative = sorted(item.relative_path for item in plan.files)
    assert relative == sorted(
        [
            "README.md",
            "stats.json",
            "assets/description_polygon_density.png",
            "assets/area_distribution.png",
            "assets/dataset-card-hero.png",
        ]
    )


def test_metadata_plan_fails_when_map_missing(tmp_path: Path) -> None:
    paths, _source_root, _data_root = _setup_two_sources(tmp_path)
    (paths.data_root / "README.md").write_text("# README")
    (paths.data_root / "stats.json").write_text("{}")
    # No map.
    with pytest.raises(PublicationError):
        _build_metadata_only_upload_plan(paths.data_root)


def test_metadata_plan_fails_when_area_histogram_missing(tmp_path: Path) -> None:
    paths, _source_root, _data_root = _setup_two_sources(tmp_path)
    (paths.data_root / "README.md").write_text("# README")
    (paths.data_root / "stats.json").write_text("{}")
    (paths.data_root / "assets").mkdir()
    (paths.data_root / "assets" / "description_polygon_density.png").write_bytes(MAP_BYTES_A)
    # No histogram.
    with pytest.raises(PublicationError):
        _build_metadata_only_upload_plan(paths.data_root)


# ---------------------------------------------------------------------------
# Allowlist hardening for assets/
# ---------------------------------------------------------------------------


def test_assets_allowlist_rejects_hidden_files(tmp_path: Path) -> None:
    paths, source_root, _data_root = _setup_two_sources(tmp_path)
    (source_root / "b.osm.pbf").unlink()
    _plant_resumable_artifact(paths, source_root, "a.osm.pbf")
    _plant_metadata(paths)
    # Add a hidden file under assets/.
    (paths.data_root / "assets" / ".hidden.png").write_bytes(MAP_BYTES_A)
    with pytest.raises(PublicationError):
        _build_per_pbf_upload_plan(paths.data_root, "a.osm.pbf")


def test_assets_allowlist_rejects_temp_files(tmp_path: Path) -> None:
    paths, source_root, _data_root = _setup_two_sources(tmp_path)
    (source_root / "b.osm.pbf").unlink()
    _plant_resumable_artifact(paths, source_root, "a.osm.pbf")
    _plant_metadata(paths)
    (paths.data_root / "assets" / "description_polygon_density.png.tmp").write_bytes(b"tmp")
    with pytest.raises(PublicationError):
        _build_per_pbf_upload_plan(paths.data_root, "a.osm.pbf")


def test_assets_allowlist_rejects_unrelated_files(tmp_path: Path) -> None:
    paths, source_root, _data_root = _setup_two_sources(tmp_path)
    (source_root / "b.osm.pbf").unlink()
    _plant_resumable_artifact(paths, source_root, "a.osm.pbf")
    _plant_metadata(paths)
    (paths.data_root / "assets" / "other.png").write_bytes(MAP_BYTES_A)
    with pytest.raises(PublicationError):
        _build_per_pbf_upload_plan(paths.data_root, "a.osm.pbf")


def test_assets_allowlist_rejects_directory_masquerading_as_file(tmp_path: Path) -> None:
    paths, source_root, _data_root = _setup_two_sources(tmp_path)
    (source_root / "b.osm.pbf").unlink()
    _plant_resumable_artifact(paths, source_root, "a.osm.pbf")
    _plant_metadata(paths)
    # Replace the map with a directory of the same name.
    map_path = paths.data_root / "assets" / "description_polygon_density.png"
    map_path.unlink()
    map_path.mkdir()
    with pytest.raises(PublicationError):
        _build_per_pbf_upload_plan(paths.data_root, "a.osm.pbf")


def test_assets_allowlist_rejects_symlink(tmp_path: Path) -> None:
    paths, source_root, _data_root = _setup_two_sources(tmp_path)
    (source_root / "b.osm.pbf").unlink()
    _plant_resumable_artifact(paths, source_root, "a.osm.pbf")
    _plant_metadata(paths)
    map_path = paths.data_root / "assets" / "description_polygon_density.png"
    map_path.unlink()
    map_path.symlink_to("/etc/passwd")
    try:
        with pytest.raises(PublicationError):
            _build_per_pbf_upload_plan(paths.data_root, "a.osm.pbf")
    finally:
        if map_path.is_symlink():
            map_path.unlink()


# ---------------------------------------------------------------------------
# Publication-state no-op detection
# ---------------------------------------------------------------------------


def test_publication_state_records_map_identity(tmp_path: Path) -> None:
    paths, _source_root, _data_root = _setup_two_sources(tmp_path)
    _plant_metadata(paths)
    plan = _build_metadata_only_upload_plan(paths.data_root)

    # The plan must include the map identity.
    map_item = next(item for item in plan.files if item.relative_path == H3_MAP_ASSET_RELATIVE_PATH)
    assert map_item.sha256 == file_sha256(
        paths.data_root / "assets" / "description_polygon_density.png"
    )
    assert map_item.size_bytes == MAP_BYTES_A.__len__()


def test_metadata_state_matches_requires_unchanged_map(tmp_path: Path) -> None:
    paths, _source_root, _data_root = _setup_two_sources(tmp_path)
    _plant_metadata(paths)
    plan = _build_metadata_only_upload_plan(paths.data_root)
    from osm_polygon_description_tag.publication.state import _write_metadata_state

    _write_metadata_state(
        paths.data_root,
        identity_sha256=plan.identity_sha256,
        readme_sha256=file_sha256(paths.data_root / "README.md"),
        stats_sha256=file_sha256(paths.data_root / "stats.json"),
        readme_size_bytes=(paths.data_root / "README.md").stat().st_size,
        stats_size_bytes=(paths.data_root / "stats.json").stat().st_size,
        h3_map_sha256=file_sha256(paths.data_root / "assets" / "description_polygon_density.png"),
        h3_map_size_bytes=(paths.data_root / "assets" / "description_polygon_density.png")
        .stat()
        .st_size,
        area_histogram_sha256=file_sha256(paths.data_root / "assets" / "area_distribution.png"),
        area_histogram_size_bytes=(paths.data_root / "assets" / "area_distribution.png")
        .stat()
        .st_size,
        dataset_card_hero_sha256=file_sha256(paths.data_root / "assets" / "dataset-card-hero.png"),
        dataset_card_hero_size_bytes=(paths.data_root / "assets" / "dataset-card-hero.png")
        .stat()
        .st_size,
        verified_revision="rev",
        completed_at="2026-01-01T00:00:00+00:00",
    )
    assert _metadata_state_matches(paths.data_root, plan) is True

    # Mutate the map.
    (paths.data_root / "assets" / "description_polygon_density.png").write_bytes(MAP_BYTES_B)
    assert _metadata_state_matches(paths.data_root, plan) is False


def test_metadata_state_matches_requires_unchanged_area_histogram(tmp_path: Path) -> None:
    """Mutating the histogram PNG must invalidate the metadata no-op too."""
    paths, _source_root, _data_root = _setup_two_sources(tmp_path)
    _plant_metadata(paths)
    plan = _build_metadata_only_upload_plan(paths.data_root)
    from osm_polygon_description_tag.publication.state import _write_metadata_state

    _write_metadata_state(
        paths.data_root,
        identity_sha256=plan.identity_sha256,
        readme_sha256=file_sha256(paths.data_root / "README.md"),
        stats_sha256=file_sha256(paths.data_root / "stats.json"),
        readme_size_bytes=(paths.data_root / "README.md").stat().st_size,
        stats_size_bytes=(paths.data_root / "stats.json").stat().st_size,
        h3_map_sha256=file_sha256(paths.data_root / "assets" / "description_polygon_density.png"),
        h3_map_size_bytes=(paths.data_root / "assets" / "description_polygon_density.png")
        .stat()
        .st_size,
        area_histogram_sha256=file_sha256(paths.data_root / "assets" / "area_distribution.png"),
        area_histogram_size_bytes=(paths.data_root / "assets" / "area_distribution.png")
        .stat()
        .st_size,
        dataset_card_hero_sha256=file_sha256(paths.data_root / "assets" / "dataset-card-hero.png"),
        dataset_card_hero_size_bytes=(paths.data_root / "assets" / "dataset-card-hero.png")
        .stat()
        .st_size,
        verified_revision="rev",
        completed_at="2026-01-01T00:00:00+00:00",
    )
    assert _metadata_state_matches(paths.data_root, plan) is True

    # Mutate the histogram.
    (paths.data_root / "assets" / "area_distribution.png").write_bytes(MAP_BYTES_B)
    assert _metadata_state_matches(paths.data_root, plan) is False


def test_state_records_map_sha_and_size_fields(tmp_path: Path) -> None:
    paths, _source_root, _data_root = _setup_two_sources(tmp_path)
    _plant_metadata(paths)
    plan = _build_metadata_only_upload_plan(paths.data_root)
    from osm_polygon_description_tag.publication.state import _write_metadata_state

    _write_metadata_state(
        paths.data_root,
        identity_sha256=plan.identity_sha256,
        readme_sha256=file_sha256(paths.data_root / "README.md"),
        stats_sha256=file_sha256(paths.data_root / "stats.json"),
        readme_size_bytes=(paths.data_root / "README.md").stat().st_size,
        stats_size_bytes=(paths.data_root / "stats.json").stat().st_size,
        h3_map_sha256=file_sha256(paths.data_root / "assets" / "description_polygon_density.png"),
        h3_map_size_bytes=(paths.data_root / "assets" / "description_polygon_density.png")
        .stat()
        .st_size,
        area_histogram_sha256=file_sha256(paths.data_root / "assets" / "area_distribution.png"),
        area_histogram_size_bytes=(paths.data_root / "assets" / "area_distribution.png")
        .stat()
        .st_size,
        dataset_card_hero_sha256=file_sha256(paths.data_root / "assets" / "dataset-card-hero.png"),
        dataset_card_hero_size_bytes=(paths.data_root / "assets" / "dataset-card-hero.png")
        .stat()
        .st_size,
        verified_revision="rev",
        completed_at="2026-01-01T00:00:00+00:00",
    )
    state = json.loads((paths.data_root / PUBLICATION_STATE_FILENAME).read_text(encoding="utf-8"))
    metadata = state["metadata"]
    assert metadata[_H3_MAP_SHA256_FIELD] == file_sha256(
        paths.data_root / "assets" / "description_polygon_density.png"
    )
    assert metadata[_H3_MAP_SIZE_FIELD] == MAP_BYTES_A.__len__()
    assert metadata["area_histogram_sha256"] == file_sha256(
        paths.data_root / "assets" / "area_distribution.png"
    )
    assert metadata["area_histogram_size_bytes"] == MAP_BYTES_A.__len__()


def test_unchanged_map_preserves_no_op(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """An unchanged map must keep the metadata no-op path active."""
    data_root = tmp_path / "generated"
    source_root = tmp_path / "raw"
    (data_root / "data").mkdir(parents=True)
    (data_root / "manifests").mkdir(parents=True)
    source_root.mkdir()
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
                schema_version=3,
                geoparquet_version="1.1.0",
                transform_algorithm_version=3,
                area_policy_sha256=current_area_policy_sha256(),
                output_algorithm_revision=current_output_algorithm_revision(),
                source=source_identity_for(source),
                output=output_identity_for(output),
                osmium_version="osmium version 1.19.1",
                dependency_versions={"pyarrow": "20.0.0"},
                code_revision=project_code_revision(),
                started_at="2026-01-01T00:00:00+00:00",
                completed_at="2026-01-01T00:00:01+00:00",
                counts=RunCounts(emitted_features=1, included_rows=1, rejections={}),
            ),
            data_root / "manifests" / f"{stem}.manifest.json",
        )
    paths = Paths(source_root=source_root, data_root=data_root)
    _plant_metadata(paths)
    # Stub the dataset card generation to a no-op so the test focuses on
    # publication state and not on the README/PNG rendering.
    monkeypatch.setattr(
        "osm_polygon_description_tag.workflow.orchestrator.generate_dataset_docs",
        lambda *args, **kwargs: {"rows": 0, "output_files": 0},
    )
    # Stub the per-PBF and metadata upload runners.
    from osm_polygon_description_tag.orchestrator import run_and_publish

    captured_uploads: list[list[str]] = []

    def upload_runner(command: list[str]) -> str:
        captured_uploads.append(list(command))
        return "rev"

    def verifier(_repo_id: str, _files: object) -> str:
        return "rev"

    # First run: must upload per-PBF, per-PBF, metadata.
    report_1 = run_and_publish(
        paths=paths,
        confirm_repo=REPO_ID,
        preflight=lambda: {"preflight": "stub", "source_count": 2},
        upload_runner=upload_runner,
        clock=lambda: "2026-01-01T00:00:00+00:00",
        exporter=lambda *a, **k: iter([]),
        verifier=verifier,
    )
    assert len(captured_uploads) == 3

    # Second run: no changes, no new uploads.
    report_2 = run_and_publish(
        paths=paths,
        confirm_repo=REPO_ID,
        preflight=lambda: {"preflight": "stub", "source_count": 2},
        upload_runner=upload_runner,
        clock=lambda: "2026-01-01T00:00:00+00:00",
        exporter=lambda *a, **k: iter([]),
        verifier=verifier,
    )
    assert len(captured_uploads) == 3  # unchanged
    for outcome in report_2.outcomes:
        assert outcome.status == "already-published"
    del report_1


def test_changed_map_forces_metadata_upload(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A changed map must invalidate the metadata no-op and force an upload."""
    data_root = tmp_path / "generated"
    source_root = tmp_path / "raw"
    (data_root / "data").mkdir(parents=True)
    (data_root / "manifests").mkdir(parents=True)
    source_root.mkdir()
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
                schema_version=3,
                geoparquet_version="1.1.0",
                transform_algorithm_version=3,
                area_policy_sha256=current_area_policy_sha256(),
                output_algorithm_revision=current_output_algorithm_revision(),
                source=source_identity_for(source),
                output=output_identity_for(output),
                osmium_version="osmium version 1.19.1",
                dependency_versions={"pyarrow": "20.0.0"},
                code_revision=project_code_revision(),
                started_at="2026-01-01T00:00:00+00:00",
                completed_at="2026-01-01T00:00:01+00:00",
                counts=RunCounts(emitted_features=1, included_rows=1, rejections={}),
            ),
            data_root / "manifests" / f"{stem}.manifest.json",
        )
    paths = Paths(source_root=source_root, data_root=data_root)
    _plant_metadata(paths)
    monkeypatch.setattr(
        "osm_polygon_description_tag.workflow.orchestrator.generate_dataset_docs",
        lambda *args, **kwargs: {"rows": 0, "output_files": 0},
    )
    from osm_polygon_description_tag.orchestrator import run_and_publish

    captured_uploads: list[list[str]] = []

    def upload_runner(command: list[str]) -> str:
        captured_uploads.append(list(command))
        return "rev"

    def verifier(_repo_id: str, _files: object) -> str:
        return "rev"

    # First run: 2 per-PBF + 1 metadata = 3 uploads.
    run_and_publish(
        paths=paths,
        confirm_repo=REPO_ID,
        preflight=lambda: {"preflight": "stub", "source_count": 2},
        upload_runner=upload_runner,
        clock=lambda: "2026-01-01T00:00:00+00:00",
        exporter=lambda *a, **k: iter([]),
        verifier=verifier,
    )
    assert len(captured_uploads) == 3

    # Change the map bytes.
    (paths.data_root / "assets" / "description_polygon_density.png").write_bytes(MAP_BYTES_B)

    # Second run: per-PBF no-op + metadata upload = 1 upload.
    run_and_publish(
        paths=paths,
        confirm_repo=REPO_ID,
        preflight=lambda: {"preflight": "stub", "source_count": 2},
        upload_runner=upload_runner,
        clock=lambda: "2026-01-01T00:00:00+00:00",
        exporter=lambda *a, **k: iter([]),
        verifier=verifier,
    )
    assert len(captured_uploads) == 4
    # The 4th upload is the metadata one and contains the new map.
    assert "assets/description_polygon_density.png" in captured_uploads[3]


# ---------------------------------------------------------------------------
# Dataset-wide plan requires assets/ (defect 2)
# ---------------------------------------------------------------------------


def test_global_plan_requires_assets_directory(tmp_path: Path) -> None:
    """``create_upload_plan`` must reject a data root without ``assets/``."""
    paths, _source_root, data_root = _setup_two_sources(tmp_path)
    (data_root / "README.md").write_text("# README")
    (data_root / "stats.json").write_text("{}")
    with pytest.raises(PublicationError, match="assets"):
        create_upload_plan(data_root)


def test_global_plan_rejects_assets_as_file(tmp_path: Path) -> None:
    """``assets`` that is a regular file (not a directory) must be rejected."""
    paths, _source_root, data_root = _setup_two_sources(tmp_path)
    (data_root / "README.md").write_text("# README")
    (data_root / "stats.json").write_text("{}")
    (data_root / "assets").write_bytes(b"NOT A DIRECTORY")
    with pytest.raises(PublicationError, match="assets"):
        create_upload_plan(data_root)


def test_global_plan_rejects_assets_symlink(tmp_path: Path) -> None:
    """An ``assets`` symlink must be rejected; the assets entry must be a real directory."""
    paths, _source_root, data_root = _setup_two_sources(tmp_path)
    (data_root / "README.md").write_text("# README")
    (data_root / "stats.json").write_text("{}")
    outside = tmp_path / "external_assets"
    outside.mkdir()
    (data_root / "assets").symlink_to(outside)
    with pytest.raises(PublicationError, match="assets"):
        create_upload_plan(data_root)


def test_global_plan_rejects_missing_map(tmp_path: Path) -> None:
    """``assets/`` exists but the canonical map file is missing."""
    paths, _source_root, data_root = _setup_two_sources(tmp_path)
    (data_root / "README.md").write_text("# README")
    (data_root / "stats.json").write_text("{}")
    (data_root / "assets").mkdir()
    with pytest.raises(PublicationError, match="H3 density map"):
        create_upload_plan(data_root)


def test_global_plan_rejects_missing_area_histogram(tmp_path: Path) -> None:
    """``assets/`` exists but the area distribution histogram is missing."""
    paths, _source_root, data_root = _setup_two_sources(tmp_path)
    (data_root / "README.md").write_text("# README")
    (data_root / "stats.json").write_text("{}")
    (data_root / "assets").mkdir()
    (data_root / "assets" / "description_polygon_density.png").write_bytes(MAP_BYTES_A)
    with pytest.raises(PublicationError, match="area distribution"):
        create_upload_plan(data_root)


def test_global_plan_includes_map_in_identity_hash(tmp_path: Path) -> None:
    """The dataset-wide plan includes the map in items and identity SHA-256."""
    paths, _source_root, data_root = _setup_two_sources(tmp_path)
    _plant_resumable_artifact(paths, paths.data_root.parent / "raw", "a.osm.pbf")
    _plant_metadata(paths)

    plan_a = create_upload_plan(data_root)
    relative_a = sorted(item.relative_path for item in plan_a.files)
    assert "assets/description_polygon_density.png" in relative_a

    # The map must be in the identity hash: a map change must change the identity.
    (data_root / "assets" / "description_polygon_density.png").write_bytes(MAP_BYTES_B)
    plan_b = create_upload_plan(data_root)
    assert plan_a.identity_sha256 != plan_b.identity_sha256
    map_item = next(
        item for item in plan_b.files if item.relative_path == H3_MAP_ASSET_RELATIVE_PATH
    )
    assert map_item.sha256 != plan_a.identity_sha256


def test_global_plan_includes_readme_stats_map_data_manifests(tmp_path: Path) -> None:
    """The dataset-wide plan contains every required entry."""
    paths, _source_root, data_root = _setup_two_sources(tmp_path)
    _plant_resumable_artifact(paths, paths.data_root.parent / "raw", "a.osm.pbf")
    _plant_resumable_artifact(paths, paths.data_root.parent / "raw", "b.osm.pbf")
    _plant_metadata(paths)

    plan = create_upload_plan(data_root)
    relative = sorted(item.relative_path for item in plan.files)
    expected = sorted(
        [
            "README.md",
            "stats.json",
            "assets/description_polygon_density.png",
            "assets/area_distribution.png",
            "assets/dataset-card-hero.png",
            "data/a.parquet",
            "data/b.parquet",
            "manifests/a.manifest.json",
            "manifests/b.manifest.json",
        ]
    )
    assert relative == expected
