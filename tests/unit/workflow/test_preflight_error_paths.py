"""Tests for preflight and verifier error paths in the orchestrator."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from osm_polygon_description_tag.config import Paths
from osm_polygon_description_tag.publication import (
    REPO_ID,
)
from osm_polygon_description_tag.workflow.orchestrator import (
    PUBLICATION_STATE_FILENAME,
    OrchestratorError,
    _execute_publication,
)
from osm_polygon_description_tag.workflow.preflight import PreflightError, default_preflight


def _setup_paths(tmp_path: Path) -> Paths:
    src = tmp_path / "raw"
    data = tmp_path / "out"
    src.mkdir()
    data.mkdir()
    return Paths(source_root=src, data_root=data)


def test_default_preflight_rejects_unreadable_source(tmp_path: Path) -> None:
    """Default preflight refuses if the source root is unreadable."""
    paths = _setup_paths(tmp_path)
    paths.source_root.chmod(0o000)
    try:
        with pytest.raises(PreflightError):
            default_preflight(
                paths,
                confirm_repo=REPO_ID,
                osmium_executable="osmium",
                hf_executable="hf",
            )
    finally:
        paths.source_root.chmod(0o755)


def test_default_preflight_rejects_unwritable_data(tmp_path: Path) -> None:
    """Default preflight refuses if the data root is not writable."""
    paths = _setup_paths(tmp_path)
    paths.data_root.chmod(0o555)
    try:
        with pytest.raises(PreflightError):
            default_preflight(
                paths,
                confirm_repo=REPO_ID,
                osmium_executable="osmium",
                hf_executable="hf",
            )
    finally:
        paths.data_root.chmod(0o755)


def test_default_preflight_rejects_wrong_confirm_repo(tmp_path: Path) -> None:
    """Default preflight rejects a mismatched confirm-repo argument."""
    paths = _setup_paths(tmp_path)
    with pytest.raises(PreflightError):
        default_preflight(
            paths,
            confirm_repo="someone/else",
            osmium_executable="osmium",
            hf_executable="hf",
        )


def test_default_preflight_rejects_no_sources(tmp_path: Path) -> None:
    """Default preflight rejects when no PBFs are found in the source root."""
    paths = _setup_paths(tmp_path)
    with pytest.raises(PreflightError):
        default_preflight(
            paths,
            confirm_repo=REPO_ID,
            osmium_executable="osmium",
            hf_executable="hf",
        )


def test_default_preflight_rejects_missing_osmium(tmp_path: Path) -> None:
    """Default preflight refuses if the osmium binary is missing."""
    paths = _setup_paths(tmp_path)
    (paths.source_root / "a.osm.pbf").write_bytes(b"x")
    with pytest.raises(PreflightError):
        default_preflight(
            paths,
            confirm_repo=REPO_ID,
            osmium_executable="/nonexistent/osmium-binary-xyz",
            hf_executable="hf",
        )


def test_default_preflight_rejects_missing_hf(tmp_path: Path) -> None:
    """Default preflight refuses if the hf binary is missing."""
    paths = _setup_paths(tmp_path)
    (paths.source_root / "a.osm.pbf").write_bytes(b"x")
    with pytest.raises(PreflightError):
        default_preflight(
            paths,
            confirm_repo=REPO_ID,
            osmium_executable="osmium",
            hf_executable="/nonexistent/hf-binary-xyz",
        )


def test_default_preflight_rejects_empty_hf_identity(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Default preflight refuses if the Hub identity is empty."""
    import osm_polygon_description_tag.workflow.preflight as orch

    paths = _setup_paths(tmp_path)
    (paths.source_root / "a.osm.pbf").write_bytes(b"x")

    fake_osmium = tmp_path / "fake-osmium"
    fake_osmium.write_text("#!/bin/sh\necho 'osmium version 1.19.1'\n", encoding="utf-8")
    fake_osmium.chmod(0o755)
    fake_hf = tmp_path / "fake-hf"
    fake_hf.write_text("#!/bin/sh\necho 'fake-user'\n", encoding="utf-8")
    fake_hf.chmod(0o755)

    name_map = {"osmium": str(fake_osmium), "hf": str(fake_hf)}

    def fake_which(name: str) -> str | None:
        return name_map.get(name)

    monkeypatch.setattr("shutil.which", fake_which)

    class _Bad:
        def whoami(self) -> object:
            return {}

        def repo_info(self, *_a: object, **_kw: object) -> object:
            class _Info:
                sha = "abc"

            return _Info()

        def auth_check(self, *_a: object, **_kw: object) -> None:
            return None

    monkeypatch.setattr(orch._huggingface_hub, "HfApi", lambda *a, **kw: _Bad())

    with pytest.raises(PreflightError, match="identity"):
        default_preflight(
            paths,
            confirm_repo=REPO_ID,
            osmium_executable="osmium",
            hf_executable="hf",
        )


def test_default_preflight_rejects_missing_sha(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Default preflight refuses if the Hub repository returns no SHA."""
    import osm_polygon_description_tag.workflow.preflight as orch

    paths = _setup_paths(tmp_path)
    (paths.source_root / "a.osm.pbf").write_bytes(b"x")

    fake_osmium = tmp_path / "fake-osmium"
    fake_osmium.write_text("#!/bin/sh\necho 'osmium version 1.19.1'\n", encoding="utf-8")
    fake_osmium.chmod(0o755)
    fake_hf = tmp_path / "fake-hf"
    fake_hf.write_text("#!/bin/sh\necho 'fake-user'\n", encoding="utf-8")
    fake_hf.chmod(0o755)

    name_map = {"osmium": str(fake_osmium), "hf": str(fake_hf)}

    def fake_which(name: str) -> str | None:
        return name_map.get(name)

    monkeypatch.setattr("shutil.which", fake_which)

    class _Empty:
        def whoami(self) -> object:
            return {"name": "fake"}

        def repo_info(self, *_a: object, **_kw: object) -> object:
            class _Info:
                sha = ""

            return _Info()

        def auth_check(self, *_a: object, **_kw: object) -> None:
            return None

    monkeypatch.setattr(orch._huggingface_hub, "HfApi", lambda *a, **kw: _Empty())

    with pytest.raises(PreflightError, match="SHA"):
        default_preflight(
            paths,
            confirm_repo=REPO_ID,
            osmium_executable="osmium",
            hf_executable="hf",
        )


def test_default_preflight_rejects_hf_api_failure(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Default preflight refuses if the HfApi raises."""
    import osm_polygon_description_tag.workflow.preflight as orch

    paths = _setup_paths(tmp_path)
    (paths.source_root / "a.osm.pbf").write_bytes(b"x")

    fake_osmium = tmp_path / "fake-osmium"
    fake_osmium.write_text("#!/bin/sh\necho 'osmium version 1.19.1'\n", encoding="utf-8")
    fake_osmium.chmod(0o755)
    fake_hf = tmp_path / "fake-hf"
    fake_hf.write_text("#!/bin/sh\necho 'fake-user'\n", encoding="utf-8")
    fake_hf.chmod(0o755)

    name_map = {"osmium": str(fake_osmium), "hf": str(fake_hf)}

    def fake_which(name: str) -> str | None:
        return name_map.get(name)

    monkeypatch.setattr("shutil.which", fake_which)

    class _Fail:
        def whoami(self) -> object:
            raise RuntimeError("hub is down")

    monkeypatch.setattr(orch._huggingface_hub, "HfApi", lambda *a, **kw: _Fail())

    with pytest.raises(PreflightError, match="Hub authentication"):
        default_preflight(
            paths,
            confirm_repo=REPO_ID,
            osmium_executable="osmium",
            hf_executable="hf",
        )


def test_atomic_state_write_raises_orchestrator_error(tmp_path: Path) -> None:
    """State writer raises OrchestratorError on invalid schema_version."""
    from osm_polygon_description_tag.workflow.orchestrator import _write_publication_state

    paths = _setup_paths(tmp_path)
    state_path = paths.data_root / PUBLICATION_STATE_FILENAME
    state_path.write_text(json.dumps({"schema_version": 999}), encoding="utf-8")
    with pytest.raises(OrchestratorError):
        _write_publication_state(
            paths.data_root,
            source_name="a.osm.pbf",
            source_sha256="00" * 32,
            output_sha256="00" * 32,
            output_bytes=0,
            remote_revision="r",
            artifact_identity="00" * 32,
            completed_at="2026-07-27T00:00:00+00:00",
        )


def test_execute_publication_rejects_empty_revision(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """An upload runner that returns empty revision is rejected."""
    from shapely.geometry import Polygon

    from osm_polygon_description_tag._resources import project_code_revision
    from osm_polygon_description_tag.manifest import (
        Manifest,
        RunCounts,
        current_area_policy_sha256,
        current_output_algorithm_revision,
        output_identity_for,
        source_identity_for,
        write_manifest,
    )
    from osm_polygon_description_tag.storage import write_geoparquet
    from tests.conftest import make_record_dict

    paths = _setup_paths(tmp_path)
    (paths.source_root / "a.osm.pbf").write_bytes(b"a-bytes")
    (paths.data_root / "README.md").write_text("# R")
    (paths.data_root / "stats.json").write_text("{}")
    (paths.data_root / "assets").mkdir()
    (paths.data_root / "assets" / "description_polygon_density.png").write_bytes(
        b"\x89PNG\r\n\x1a\n" + b"map" * 1024
    )
    (paths.data_root / "assets" / "area_distribution.png").write_bytes(
        b"\x89PNG\r\n\x1a\n" + b"hist" * 1024
    )
    (paths.data_root / "data").mkdir()
    (paths.data_root / "manifests").mkdir()
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
    write_manifest(
        Manifest(
            manifest_schema_version=2,
            schema_version=2,
            geoparquet_version="1.1.0",
            transform_algorithm_version=2,
            area_policy_sha256=current_area_policy_sha256(),
            output_algorithm_revision=current_output_algorithm_revision(),
            source=source_identity_for(paths.source_root / "a.osm.pbf"),
            output=output_identity_for(paths.data_root / "data" / "a.parquet"),
            osmium_version="osmium version 1.19.1",
            dependency_versions={"pyarrow": "20.0.0"},
            code_revision=project_code_revision(),
            started_at="2026-07-27T00:00:00+00:00",
            completed_at="2026-07-27T00:01:00+00:00",
            counts=RunCounts(emitted_features=1, included_rows=1, rejections={}),
        ),
        paths.data_root / "manifests" / "a.manifest.json",
    )

    def bad_runner(command: list[str]) -> str:
        return ""

    with pytest.raises(OrchestratorError):
        _execute_publication(
            paths,
            type("S", (), {"name": "a.osm.pbf"})(),
            verifier=lambda *a, **kw: "r",
            timeout=None,
            upload_runner=bad_runner,
        )
