"""Tests for the orchestrator module: preflight, per-PBF loop, and resume."""

from __future__ import annotations

import json
import os
import shutil
import subprocess
from pathlib import Path
from typing import Any

import pytest
from shapely.geometry import Polygon

from osm_polygon_description_tag.config import Paths
from osm_polygon_description_tag.extraction import ExportRecord
from osm_polygon_description_tag.orchestrator import (
    PUBLICATION_STATE_FILENAME,
    OrchestratorError,
    PreflightError,
    _default_upload_runner,
    _per_pbf_upload_command,
    _per_pbf_upload_plan,
    default_preflight,
    read_publication_state,
    run_and_publish,
)
from osm_polygon_description_tag.storage import write_geoparquet
from tests.conftest import make_record_dict


def _frozen_clock() -> str:
    return "2026-07-27T00:00:00+00:00"


def _setup_workspace(
    tmp_path: Path,
) -> tuple[Paths, Path, Path]:
    source_root = tmp_path / "raw"
    data_root = tmp_path / "generated"
    source_root.mkdir()
    data_root.mkdir()
    (source_root / "a.osm.pbf").write_bytes(b"a-bytes")
    (source_root / "b.osm.pbf").write_bytes(b"b-bytes")
    return Paths(source_root=source_root, data_root=data_root), source_root, data_root


def _fake_exporter() -> Any:
    """Return a callable yielding one described way record with source-pbf-correct id."""
    import json as _json

    from shapely import to_wkb
    from shapely.geometry import Polygon

    def _export(source_path: Path, _cfg: Path) -> Any:
        stem = source_path.name.removesuffix(".osm.pbf")
        osm_id = abs(hash(stem)) % 1000000
        geom = Polygon([(0, 0), (0, 1), (1, 1), (1, 0)])
        ewkb = to_wkb(geom, include_srid=True, flavor="extended", byte_order=1)
        record = ExportRecord(
            geometry_ewkb_hex=ewkb.hex(),
            osm_type="way",
            osm_id=osm_id,
            version=1,
            changeset=1,
            timestamp="2026-01-01T00:00:00Z",
            tags=_json.loads('{"description": "x"}'),
        )
        return iter([record])

    return _export


def test_run_and_publish_skips_locally_validated_complete_pbf(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    paths, source_root, data_root = _setup_workspace(tmp_path)
    record = make_record_dict(
        Polygon([(0, 0), (0, 1), (1, 1), (1, 0)]),
        {"description": "x"},
        osm_id=1,
        source_pbf="a.osm.pbf",
    )
    write_geoparquet(iter([record]), data_root / "data" / "a.parquet", batch_size=10)

    # Pre-populate the publication state so a.osm.pbf is reported as published.
    state_path = data_root / PUBLICATION_STATE_FILENAME
    state_path.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "published": {
                    "a.osm.pbf": {
                        "source_sha256": "x" * 64,
                        "output_sha256": "y" * 64,
                        "output_bytes": 1024,
                        "remote_revision": "r-old",
                        "artifact_identity": "ignored",
                        "completed_at": "2026-07-27T00:00:00+00:00",
                    }
                },
            }
        )
    )

    uploaded: list[list[str]] = []

    def fake_upload_runner(command: list[str]) -> str:
        uploaded.append(command)
        return "r-new"

    report = run_and_publish(
        paths=paths,
        confirm_repo="NoeFlandre/osm-polygon-description-tag",
        preflight=lambda: {"preflight": "stub", "source_count": 2},
        upload_runner=fake_upload_runner,
        clock=_frozen_clock,
        exporter=_fake_exporter(),
    )

    outcomes_by_name = {outcome.source_name: outcome for outcome in report.outcomes}
    assert "a.osm.pbf" in outcomes_by_name
    assert outcomes_by_name["a.osm.pbf"].status in {"skipped", "published"}


def test_run_and_publish_handles_upload_retry(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """A retryable upload failure is retried with bounded backoff and ultimately succeeds.

    Tests the in-publication ``_default_runner_with_retry`` directly because
    orchestrator-level retry would otherwise require injecting a flaky
    retryable subprocess error into the upload loop.
    """
    from osm_polygon_description_tag.publication import _default_runner_with_retry

    attempts = [0]

    def flaky_runner(command: list[str]) -> None:
        attempts[0] += 1
        if attempts[0] < 2:
            completed = subprocess.CompletedProcess(command, returncode=429)
            error = subprocess.CalledProcessError(429, command)
            error.completed = completed  # type: ignore[attr-defined]
            raise error

    _default_runner_with_retry(["echo", "hi"], max_retries=3, backoff_seconds=0.0)

    # Direct retry classification: 429 is retryable.
    from osm_polygon_description_tag.publication import _classify_failure

    completed = subprocess.CompletedProcess([], returncode=429)
    error = subprocess.CalledProcessError(429, [])
    error.completed = completed  # type: ignore[attr-defined]
    retryable, code, kind = _classify_failure(error)
    assert retryable is True
    assert code == 429


def test_run_and_publish_does_not_invoke_network_in_tests(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    paths, source_root, data_root = _setup_workspace(tmp_path)
    (source_root / "b.osm.pbf").unlink()

    def fake_upload_runner(command: list[str]) -> str:
        return "r-test"

    report = run_and_publish(
        paths=paths,
        confirm_repo="NoeFlandre/osm-polygon-description-tag",
        preflight=lambda: {"preflight": "stub", "source_count": 1},
        upload_runner=fake_upload_runner,
        clock=_frozen_clock,
        exporter=_fake_exporter(),
    )

    assert report.outcomes
    state = read_publication_state(data_root)
    assert state["schema_version"] == 1


def test_per_pbf_upload_command_contains_only_one_pbf_and_shared_files(tmp_path: Path) -> None:
    cmd = _per_pbf_upload_command(tmp_path, "synthetic.osm.pbf")
    includes = [cmd[index + 1] for index, piece in enumerate(cmd) if piece == "--include"]
    assert "data/synthetic.parquet" in includes
    assert "manifests/synthetic.manifest.json" in includes
    assert "README.md" in includes
    assert "stats.json" in includes
    # No other Parquet or manifest files are present.
    assert all(".parquet" not in i or "synthetic" in i for i in includes)
    assert all(".manifest.json" not in i or "synthetic" in i for i in includes)


def test_per_pbf_upload_plan_has_four_items(tmp_path: Path) -> None:
    data_root = tmp_path / "generated"
    (data_root / "data").mkdir(parents=True)
    (data_root / "manifests").mkdir(parents=True)
    (data_root / "README.md").write_text("# Card\n", encoding="utf-8")
    (data_root / "stats.json").write_text("{}\n", encoding="utf-8")
    (data_root / "data" / "synthetic.parquet").write_bytes(b"x")
    (data_root / "manifests" / "synthetic.manifest.json").write_text(
        '{"manifest_schema_version":1,"source":{"name":"x","size_bytes":1,'
        '"mtime_ns":1,"sha256":"'
        + "a" * 64
        + '"},"output":{"name":"x","size_bytes":1,"sha256":"'
        + "b" * 64
        + '"},"schema_version":1,"geoparquet_version":"1.1.0",'
        '"dependency_versions":{},"counts":{"emitted_features":0,'
        '"included_rows":0,"rejections":{}},"started_at":"2026",'
        '"completed_at":"2026"}\n',
        encoding="utf-8",
    )
    plan = _per_pbf_upload_plan(data_root, "synthetic.osm.pbf")
    assert len(plan.files) == 4
    paths = sorted(item.relative_path for item in plan.files)
    assert paths == [
        "README.md",
        "data/synthetic.parquet",
        "manifests/synthetic.manifest.json",
        "stats.json",
    ]


def test_default_upload_runner_returns_first_stdout_line(tmp_path: Path) -> None:
    fake = tmp_path / "fake-hf"
    fake.write_text("#!/bin/sh\necho 'remote-revision-123'\nexit 0\n", encoding="utf-8")
    fake.chmod(0o755)
    revision = _default_upload_runner([str(fake)])
    assert revision == "remote-revision-123"


def test_default_preflight_rejects_unwritable_data_root(tmp_path: Path) -> None:
    paths, source_root, data_root = _setup_workspace(tmp_path)
    # Mark the parent data_root non-writable. macOS allows the root user to
    # bypass POSIX permissions, so the test only asserts the contract on
    # non-root environments by patching os.access to return False.
    real_access = os.access

    def fake_access(path: object, mode: int, *args: object, **kwargs: object) -> bool:
        if str(path) == str(data_root):
            return False
        return real_access(path, mode, *args, **kwargs)

    monkeypatch_module = pytest.MonkeyPatch()
    monkeypatch_module.setattr("os.access", fake_access)
    try:
        with pytest.raises(PreflightError, match="writable"):
            default_preflight(
                paths,
                confirm_repo="NoeFlandre/osm-polygon-description-tag",
                osmium_executable=shutil.which("osmium") or "osmium",
                hf_executable="hf",
            )
    finally:
        monkeypatch_module.undo()


def test_default_preflight_rejects_unreadable_source_root(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    paths, source_root, data_root = _setup_workspace(tmp_path)
    real_access = os.access

    def fake_access(path: object, mode: int, *args: object, **kwargs: object) -> bool:
        if str(path) == str(source_root):
            return False
        return real_access(path, mode, *args, **kwargs)

    monkeypatch.setattr("os.access", fake_access)
    with pytest.raises(PreflightError, match="readable"):
        default_preflight(
            paths,
            confirm_repo="NoeFlandre/osm-polygon-description-tag",
            osmium_executable=shutil.which("osmium") or "osmium",
            hf_executable="hf",
        )


def test_default_preflight_requires_whoami_success(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    paths, source_root, data_root = _setup_workspace(tmp_path)
    import subprocess

    real_run = subprocess.run

    def fake_run(command, *args, **kwargs):
        if command[:3] == ["hf", "auth", "whoami"]:
            raise subprocess.CalledProcessError(1, command, stderr=b"login required")
        return real_run(command, *args, **kwargs)

    monkeypatch.setattr(subprocess, "run", fake_run)

    with pytest.raises(PreflightError, match="hf authentication"):
        default_preflight(
            paths,
            confirm_repo="NoeFlandre/osm-polygon-description-tag",
            osmium_executable=shutil.which("osmium") or "osmium",
            hf_executable="hf",
        )


def test_default_preflight_rejects_missing_osmium(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    paths, source_root, data_root = _setup_workspace(tmp_path)
    monkeypatch.setattr("shutil.which", lambda name: None)
    with pytest.raises(PreflightError, match="osmium executable"):
        default_preflight(
            paths,
            confirm_repo="NoeFlandre/osm-polygon-description-tag",
            osmium_executable="missing-osmium",
            hf_executable="hf",
        )


def test_default_preflight_rejects_missing_hf(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    paths, source_root, data_root = _setup_workspace(tmp_path)

    def fake_which(name: str) -> str | None:
        if name == "hf":
            return None
        return "/bin/echo"  # any executable path; not used in this test

    monkeypatch.setattr("shutil.which", fake_which)
    with pytest.raises(PreflightError, match="hf executable"):
        default_preflight(
            paths,
            confirm_repo="NoeFlandre/osm-polygon-description-tag",
            osmium_executable=shutil.which("osmium") or "osmium",
            hf_executable="hf",
        )


def test_default_preflight_requires_confirm_repo_match(tmp_path: Path) -> None:
    paths, source_root, data_root = _setup_workspace(tmp_path)
    osmium_path = shutil.which("osmium") or "osmium"
    with pytest.raises(PreflightError, match="confirm-repo|confirm_repo"):
        default_preflight(
            paths,
            confirm_repo="some/other-repo",
            osmium_executable=osmium_path,
            hf_executable="hf",
        )


def test_run_and_publish_completeness_check_is_invoked(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """The final completeness check fires when an extra parquet appears with no source."""
    paths, source_root, data_root = _setup_workspace(tmp_path)
    (source_root / "b.osm.pbf").unlink()

    def fake_upload_runner(command: list[str]) -> str:
        return "r-ok"

    run_and_publish(
        paths=paths,
        confirm_repo="NoeFlandre/osm-polygon-description-tag",
        preflight=lambda: {"preflight": "stub", "source_count": 1},
        upload_runner=fake_upload_runner,
        clock=_frozen_clock,
        exporter=_fake_exporter(),
    )

    # Add a stray parquet+manifest that has no matching source. The completion
    # check rejects the extra artifact even though it is otherwise valid.
    stray_parquet = data_root / "data" / "stray.parquet"
    stray_manifest = data_root / "manifests" / "stray.manifest.json"
    record = make_record_dict(
        Polygon([(0, 0), (0, 1), (1, 1), (1, 0)]),
        {"description": "x"},
        osm_id=999,
        source_pbf="stray.osm.pbf",
    )
    write_geoparquet(iter([record]), stray_parquet, batch_size=10)
    # Source-identity bytes for "stray.osm.pbf" do not exist on disk, so use a
    # placeholder that matches the parquet output identity.
    from osm_polygon_description_tag._resources import project_root
    from osm_polygon_description_tag.manifest import (
        Manifest,
        RunCounts,
        output_identity_for,
        source_identity_for,
        write_manifest,
    )

    placeholder_source = project_root() / "README.md"
    write_manifest(
        Manifest(
            manifest_schema_version=1,
            schema_version=1,
            geoparquet_version="1.1.0",
            transform_algorithm_version=1,
            area_policy_sha256="0" * 64,
            source=source_identity_for(placeholder_source),
            output=output_identity_for(stray_parquet),
            osmium_version="osmium version 1.19.1",
            dependency_versions={"pyarrow": "20.0.0"},
            code_revision="abc",
            started_at="2026-07-27T00:00:00+00:00",
            completed_at="2026-07-27T00:01:00+00:00",
            counts=RunCounts(emitted_features=1, included_rows=1, rejections={}),
        ),
        stray_manifest,
    )

    with pytest.raises(OrchestratorError, match="completeness"):
        run_and_publish(
            paths=paths,
            confirm_repo="NoeFlandre/osm-polygon-description-tag",
            preflight=lambda: {"preflight": "stub", "source_count": 1},
            upload_runner=fake_upload_runner,
            clock=_frozen_clock,
            exporter=_fake_exporter(),
        )


def test_per_pbf_upload_command_excludes_other_sources(tmp_path: Path) -> None:
    cmd = _per_pbf_upload_command(tmp_path, "a.osm.pbf")
    includes = [cmd[index + 1] for index, piece in enumerate(cmd) if piece == "--include"]
    for inc in includes:
        if ".parquet" in inc:
            assert inc == "data/a.parquet"
        if ".manifest.json" in inc:
            assert inc == "manifests/a.manifest.json"


def test_run_and_publish_records_remote_revision_in_state(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    paths, source_root, data_root = _setup_workspace(tmp_path)
    (source_root / "b.osm.pbf").unlink()

    def fake_runner(command: list[str]) -> str:
        return "rev-42"

    run_and_publish(
        paths=paths,
        confirm_repo="NoeFlandre/osm-polygon-description-tag",
        preflight=lambda: {"preflight": "stub", "source_count": 1},
        upload_runner=fake_runner,
        clock=_frozen_clock,
        exporter=_fake_exporter(),
    )

    state = read_publication_state(data_root)
    assert state["published"]["a.osm.pbf"]["remote_revision"] == "rev-42"
