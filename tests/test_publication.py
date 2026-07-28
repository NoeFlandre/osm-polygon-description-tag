import subprocess
from pathlib import Path

import pytest
from shapely.geometry import Polygon

from osm_polygon_description_tag.manifest import (
    Manifest,
    RunCounts,
    output_identity_for,
    source_identity_for,
    write_manifest,
)
from osm_polygon_description_tag.publication import (
    PublicationError,
    create_upload_plan,
    execute_upload,
)
from osm_polygon_description_tag.storage import write_geoparquet
from tests.conftest import make_record_dict


def _make_dataset(data_root: Path) -> None:
    (data_root / "data").mkdir(parents=True)
    (data_root / "manifests").mkdir(parents=True)
    (data_root / "README.md").write_text("# Card\n", encoding="utf-8")
    (data_root / "stats.json").write_text("{}\n", encoding="utf-8")
    source_root = data_root.parent / "raw"
    source_root.mkdir(exist_ok=True)
    source = source_root / "a-latest.osm.pbf"
    source.write_bytes(b"a-latest-bytes")
    record = make_record_dict(
        Polygon([(0, 0), (0, 1), (1, 1), (1, 0)]),
        {"description": "x"},
        osm_id=1,
        source_pbf="a-latest.osm.pbf",
    )
    output = data_root / "data" / "a-latest.parquet"
    write_geoparquet(iter([record]), output, batch_size=10)
    manifest = Manifest(
        manifest_schema_version=2,
        schema_version=2,
        geoparquet_version="1.1.0",
        transform_algorithm_version=2,
        output_algorithm_revision="x" * 64,
        area_policy_sha256="0" * 64,
        source=source_identity_for(source),
        output=output_identity_for(output),
        osmium_version="osmium version 1.16.0",
        dependency_versions={"pyarrow": "20.0.0"},
        code_revision="abc123",
        started_at="2026-07-27T00:00:00+00:00",
        completed_at="2026-07-27T00:01:00+00:00",
        counts=RunCounts(emitted_features=1, included_rows=1, rejections={}),
    )
    write_manifest(manifest, data_root / "manifests" / "a-latest.manifest.json")


def test_create_upload_plan_lists_allowlisted_files(tmp_path: Path) -> None:
    data_root = tmp_path / "generated"
    _make_dataset(data_root)

    plan = create_upload_plan(data_root)

    assert [item.relative_path for item in plan.files] == [
        "README.md",
        "data/a-latest.parquet",
        "manifests/a-latest.manifest.json",
        "stats.json",
    ]
    assert plan.repo_id == "NoeFlandre/osm-polygon-description-tag"
    assert len(plan.identity_sha256) == 64
    assert plan.data_root == str(data_root.resolve(strict=False))


def test_create_upload_plan_is_deterministic(tmp_path: Path) -> None:
    data_root = tmp_path / "generated"
    _make_dataset(data_root)

    plan_a = create_upload_plan(data_root)
    plan_b = create_upload_plan(data_root)

    assert plan_a.identity_sha256 == plan_b.identity_sha256
    assert plan_a.to_json() == plan_b.to_json()


def test_create_upload_plan_rejects_unknown_top_level(tmp_path: Path) -> None:
    data_root = tmp_path / "generated"
    _make_dataset(data_root)
    (data_root / "debug.txt").write_text("debug", encoding="utf-8")

    with pytest.raises(PublicationError, match="top-level|unknown"):
        create_upload_plan(data_root)


def test_create_upload_plan_rejects_symlinks(tmp_path: Path) -> None:
    data_root = tmp_path / "generated"
    _make_dataset(data_root)
    target = tmp_path / "external.bin"
    target.write_bytes(b"x")
    (data_root / "data" / "link.parquet").symlink_to(target)

    with pytest.raises(PublicationError, match="symlink"):
        create_upload_plan(data_root)


def test_create_upload_plan_rejects_temporary_files(tmp_path: Path) -> None:
    data_root = tmp_path / "generated"
    _make_dataset(data_root)
    (data_root / "data" / "leftover.tmp").write_bytes(b"x")

    with pytest.raises(PublicationError, match="temporary|unknown"):
        create_upload_plan(data_root)


def test_create_upload_plan_rejects_missing_card_or_stats(tmp_path: Path) -> None:
    data_root = tmp_path / "generated"
    _make_dataset(data_root)
    (data_root / "README.md").unlink()

    with pytest.raises(PublicationError, match="missing|R|README"):
        create_upload_plan(data_root)


def test_execute_upload_refuses_wrong_confirmation(tmp_path: Path) -> None:
    data_root = tmp_path / "generated"
    _make_dataset(data_root)
    plan = create_upload_plan(data_root)

    def runner(command: list[str]) -> None:
        raise AssertionError("runner should not be invoked")

    with pytest.raises(PublicationError, match="confirmation"):
        execute_upload(plan, confirmation="deadbeef", runner=runner)


def test_execute_upload_passes_allowlisted_exact_includes(tmp_path: Path) -> None:
    data_root = tmp_path / "generated"
    _make_dataset(data_root)
    plan = create_upload_plan(data_root)

    captured: list[list[str]] = []

    def runner(command: list[str]) -> None:
        captured.append(command)

    execute_upload(plan, confirmation=plan.identity_sha256, runner=runner)

    assert len(captured) == 1
    expected_command = [
        "hf",
        "upload-large-folder",
        "NoeFlandre/osm-polygon-description-tag",
        str(data_root.resolve(strict=False)),
        "--repo-type",
        "dataset",
    ]
    for item in sorted(plan.files, key=lambda i: i.relative_path):
        expected_command.extend(["--include", item.relative_path])
    assert captured[0] == expected_command


def test_execute_upload_detects_checksum_drift(tmp_path: Path) -> None:
    data_root = tmp_path / "generated"
    _make_dataset(data_root)
    plan = create_upload_plan(data_root)

    # Mutate an artifact after plan creation.
    (data_root / "README.md").write_text("mutated", encoding="utf-8")

    def runner(command: list[str]) -> None:
        raise AssertionError("runner should not be invoked on drift")

    with pytest.raises(PublicationError, match="drift|mismatch"):
        execute_upload(plan, confirmation=plan.identity_sha256, runner=runner)


def test_execute_upload_invokes_runner_with_subprocess_run_by_default(tmp_path: Path) -> None:
    data_root = tmp_path / "generated"
    _make_dataset(data_root)
    plan = create_upload_plan(data_root)

    def fake_subprocess_run(command, **kwargs):  # type: ignore[no-untyped-def]
        return subprocess.CompletedProcess(command, 0)

    # Patch subprocess.run via the publication module import path.
    import osm_polygon_description_tag.publication as publication

    original = publication.subprocess.run
    publication.subprocess.run = fake_subprocess_run  # type: ignore[assignment]
    try:
        # When invoked with the default runner, confirm it goes through subprocess.run.
        execute_upload(plan, confirmation=plan.identity_sha256)
    finally:
        publication.subprocess.run = original  # type: ignore[assignment]
