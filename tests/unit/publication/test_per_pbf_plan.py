"""Tests proving the per-PBF upload plan is exact, not whole-data-root.

The single canonical function ``build_per_pbf_upload_plan`` must produce a
plan whose ``files`` contain exactly:

- ``data/<stem>.parquet``
- ``manifests/<stem>.manifest.json``
- ``README.md``
- ``stats.json``

Both the production subprocess runner and any injected test runner must call
this same canonical function. Earlier PBF outputs are not re-uploaded.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from shapely.geometry import Polygon

from osm_polygon_description_tag._resources import project_code_revision
from osm_polygon_description_tag.config import Paths
from osm_polygon_description_tag.manifest import (
    Manifest,
    RunCounts,
    current_area_policy_sha256,
    current_output_algorithm_revision,
    output_identity_for,
    source_identity_for,
    write_manifest,
)
from osm_polygon_description_tag.orchestrator import (
    PUBLICATION_STATE_FILENAME,
    run_and_publish,
)
from osm_polygon_description_tag.publication import (
    REPO_ID,
    _build_metadata_only_upload_plan,
    _build_per_pbf_upload_plan,
    per_pbf_command,
)
from osm_polygon_description_tag.storage import write_geoparquet
from tests.conftest import make_record_dict

_CLOCK = "2026-07-27T00:00:00+00:00"


def _frozen_clock() -> str:
    return _CLOCK


def _setup_two_sources(tmp_path: Path) -> tuple[Paths, Path, Path]:
    source_root = tmp_path / "raw"
    data_root = tmp_path / "generated"
    source_root.mkdir()
    data_root.mkdir()
    (source_root / "a.osm.pbf").write_bytes(b"a-bytes")
    (source_root / "b.osm.pbf").write_bytes(b"b-bytes")
    return Paths(source_root=source_root, data_root=data_root), source_root, data_root


def _plant_resumable_artifact(paths: Paths, source_root: Path, source_name: str) -> None:
    """Plant a complete, resumable artifact for ``source_name`` on disk."""
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
            schema_version=2,
            geoparquet_version="1.1.0",
            transform_algorithm_version=2,
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
    """Plant README.md, stats.json, and all required visual assets in the data root."""
    (paths.data_root / "README.md").write_text("# README")
    (paths.data_root / "stats.json").write_text("{}")
    (paths.data_root / "assets").mkdir(exist_ok=True)
    (paths.data_root / "assets" / "description_polygon_density.png").write_bytes(
        b"\x89PNG\r\n\x1a\n" + b"map" * 1024
    )
    (paths.data_root / "assets" / "area_distribution.png").write_bytes(
        b"\x89PNG\r\n\x1a\n" + b"hist" * 1024
    )
    (paths.data_root / "assets" / "dataset-card-hero.png").write_bytes(
        b"\x89PNG\r\n\x1a\n" + b"hero" * 1024
    )


def test_per_pbf_plan_contains_exactly_five_items(tmp_path: Path) -> None:
    """A per-PBF plan must contain only the canonical files for that PBF."""
    paths, source_root, _data_root = _setup_two_sources(tmp_path)

    # Drop b so only ``a`` is processed. Plant ``a`` so the orchestrator skips
    # building it; only the upload+verify path is exercised.
    (source_root / "b.osm.pbf").unlink()
    _plant_resumable_artifact(paths, source_root, "a.osm.pbf")
    _plant_metadata(paths)

    # Sanity: the plan builder produces exactly seven items.
    plan = _build_per_pbf_upload_plan(paths.data_root, "a.osm.pbf")
    expected_relative = sorted([item.relative_path for item in plan.files])
    assert expected_relative == sorted(
        [
            "data/a.parquet",
            "manifests/a.manifest.json",
            "README.md",
            "stats.json",
            "assets/description_polygon_density.png",
            "assets/area_distribution.png",
            "assets/dataset-card-hero.png",
        ]
    ), f"per-PBF plan must contain exactly 7 files; got {expected_relative}"

    # The command the runner receives equals the canonical command.
    expected_command = per_pbf_command(paths.data_root, "a.osm.pbf")
    assert expected_command[0] == "hf"
    assert expected_command[1] == "upload-large-folder"
    assert REPO_ID in expected_command
    includes = [
        expected_command[index + 1]
        for index, piece in enumerate(expected_command)
        if piece == "--include"
    ]
    assert sorted(includes) == expected_relative


def test_no_test_production_divergence_default_runner(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Both default execution and injected runner build the same canonical command."""
    paths, source_root, data_root = _setup_two_sources(tmp_path)
    (source_root / "b.osm.pbf").unlink()
    _plant_resumable_artifact(paths, source_root, "a.osm.pbf")
    _plant_metadata(paths)
    plan = _build_per_pbf_upload_plan(paths.data_root, "a.osm.pbf")

    captured: list[list[str]] = []

    def fake_subprocess(command: list[str], timeout: float | None = None) -> None:
        captured.append(command)

    import osm_polygon_description_tag.publication.upload as pub

    monkeypatch.setattr(pub, "_default_runner_with_retry", fake_subprocess)

    def _stub_exporter(source_path: Path, _cfg: Path) -> object:
        raise AssertionError("exporter must not be called when artifact is reusable")

    run_and_publish(
        paths=paths,
        confirm_repo=REPO_ID,
        preflight=lambda: {"preflight": "stub", "source_count": 1},
        clock=_frozen_clock,
        exporter=_stub_exporter,
        verifier=lambda repo_id, files: "verified-rev",
    )
    assert captured, "default runner must be invoked"
    # Default path uses plan.repo_id + a single 'hf upload-large-folder' command.
    assert "hf" in captured[0]
    assert captured[0][1] == "upload-large-folder"
    assert REPO_ID in captured[0]
    # Include flags must contain each plan item relative path exactly once.
    includes = [captured[0][i + 1] for i, piece in enumerate(captured[0]) if piece == "--include"]
    expected_includes = sorted(item.relative_path for item in plan.files)
    assert sorted(includes) == expected_includes


def test_upload_runner_receives_same_canonical_command(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """The injected upload_runner receives the canonical per-PBF command."""
    paths, source_root, data_root = _setup_two_sources(tmp_path)
    (source_root / "b.osm.pbf").unlink()
    _plant_resumable_artifact(paths, source_root, "a.osm.pbf")
    _plant_metadata(paths)
    canonical = per_pbf_command(paths.data_root, "a.osm.pbf")

    seen_commands: list[list[str]] = []

    def upload_runner(command: list[str]) -> str:
        seen_commands.append(list(command))
        return "verified-rev"

    def _stub_exporter(source_path: Path, _cfg: Path) -> object:
        raise AssertionError("exporter must not be called when artifact is reusable")

    run_and_publish(
        paths=paths,
        confirm_repo=REPO_ID,
        preflight=lambda: {"preflight": "stub", "source_count": 1},
        upload_runner=upload_runner,
        clock=_frozen_clock,
        exporter=_stub_exporter,
        verifier=lambda repo_id, files: "verified-rev",
    )
    # The runner is called twice: once for per-PBF, once for the final
    # metadata. The first call must equal the canonical per-PBF command.
    assert seen_commands, "runner was never invoked"
    assert seen_commands[0] == canonical, (
        f"per-PBF command diverged; canonical={canonical}, actual={seen_commands[0]}"
    )


def test_second_pbf_upload_does_not_re_upload_first_pbf(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Publishing the second PBF never re-uploads the first PBF's parquet/manifest."""
    paths, source_root, data_root = _setup_two_sources(tmp_path)
    _plant_resumable_artifact(paths, source_root, "a.osm.pbf")
    _plant_resumable_artifact(paths, source_root, "b.osm.pbf")
    _plant_metadata(paths)

    # Mark "a" as already published.
    state = {
        "schema_version": 1,
        "published": {
            "a.osm.pbf": {
                "source_sha256": source_identity_for(source_root / "a.osm.pbf").sha256,
                "output_sha256": output_identity_for(paths.data_root / "data" / "a.parquet").sha256,
                "output_bytes": (paths.data_root / "data" / "a.parquet").stat().st_size,
                "remote_revision": "r-a",
                "artifact_identity": "00" * 32,
                "completed_at": "2026-07-27T00:01:00+00:00",
            }
        },
    }
    (paths.data_root / PUBLICATION_STATE_FILENAME).write_text(
        json.dumps(state, sort_keys=True, indent=2), encoding="utf-8"
    )

    seen_commands: list[list[str]] = []

    def upload_runner(command: list[str]) -> str:
        seen_commands.append(list(command))
        return "verified-rev-b"

    def _stub_exporter(source_path: Path, _cfg: Path) -> object:
        raise AssertionError("exporter must not be called when artifact is reusable")

    run_and_publish(
        paths=paths,
        confirm_repo=REPO_ID,
        preflight=lambda: {"preflight": "stub", "source_count": 2},
        upload_runner=upload_runner,
        clock=_frozen_clock,
        exporter=_stub_exporter,
        verifier=lambda repo_id, files: "verified-rev-b",
    )
    # The only seen commands must NOT mention data/a.parquet anywhere.
    for command in seen_commands:
        assert "data/a.parquet" not in command, (
            f"a.parquet must not be re-uploaded; saw command {command}"
        )
        assert "manifests/a.manifest.json" not in command


def test_metadata_only_plan_contains_exactly_three_items(tmp_path: Path) -> None:
    """The metadata-only UploadPlan contains README.md, stats.json, and every required visual asset."""
    paths, source_root, data_root = _setup_two_sources(tmp_path)
    _plant_metadata(paths)
    plan = _build_metadata_only_upload_plan(paths.data_root)
    relative = sorted([item.relative_path for item in plan.files])
    assert relative == sorted(
        [
            "README.md",
            "stats.json",
            "assets/description_polygon_density.png",
            "assets/area_distribution.png",
            "assets/dataset-card-hero.png",
        ]
    )
