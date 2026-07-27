"""Synthetic dry-run of ``run-and-publish`` with an injected fake publisher and interrupt/restart.

This test exercises the public ``run-and_publish`` orchestrator end-to-end:

1. Builds two synthetic source PBF records.
2. Uploads one PBF and intentionally interrupts before the second upload.
3. Restarts and verifies the second upload runs; the first is skipped.
4. Asserts the publication state matches reality.
"""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest

from osm_polygon_description_tag.config import Paths
from osm_polygon_description_tag.orchestrator import (
    PUBLICATION_STATE_FILENAME,
    OrchestratorError,
    read_publication_state,
    run_and_publish,
)


def _fake_exporter_factory(records_per_source: dict[str, int]):
    """Return a closure that yields ``records_per_source[stem]`` valid records."""

    def _export(source_path: Path, _cfg: Path):  # type: ignore[no-untyped-def]
        from shapely import to_wkb
        from shapely.geometry import Polygon

        from osm_polygon_description_tag.extraction import ExportRecord

        stem = source_path.name.removesuffix(".osm.pbf")
        n = records_per_source[stem]
        records = []
        for i in range(n):
            geom = Polygon([(0, 0), (0, 1), (1, 1), (1, 0)])
            ewkb = to_wkb(geom, include_srid=True, flavor="extended", byte_order=1)
            records.append(
                ExportRecord(
                    geometry_ewkb_hex=ewkb.hex(),
                    osm_type="way",
                    osm_id=10_000 + abs(hash(stem)) % 1000 + i,
                    version=1,
                    changeset=1,
                    timestamp="2026-01-01T00:00:00Z",
                    tags=json.loads('{"description": "x"}'),
                )
            )
        return iter(records)

    return _export


def _fake_preflight() -> dict[str, object]:
    return {"preflight": "stub", "source_count": 2}


def _frozen_clock() -> str:
    return "2026-07-27T00:00:00+00:00"


def test_run_and_publish_resume_after_interrupt(tmp_path: Path) -> None:
    """The full flow: build one, interrupt, restart, build the second."""
    source_root = tmp_path / "raw"
    data_root = tmp_path / "generated"
    source_root.mkdir()
    data_root.mkdir()
    (source_root / "a.osm.pbf").write_bytes(b"a-bytes")
    (source_root / "b.osm.pbf").write_bytes(b"b-bytes")
    paths = Paths(source_root=source_root, data_root=data_root)

    records = {"a": 1, "b": 1}
    exporter = _fake_exporter_factory(records)

    # First run: succeed for "a", then "fail" for "b" to simulate interrupt.
    uploaded: list[str] = []

    def fail_on_b(command: list[str]) -> str:
        # Identify the source by the parquet include flag.
        for inc in command:
            if inc.startswith("data/"):
                uploaded.append(inc)
        if "data/b.parquet" in command:
            raise subprocess.CalledProcessError(1, command)
        return "r-a"

    with pytest.raises(OrchestratorError):
        run_and_publish(
            paths=paths,
            confirm_repo="NoeFlandre/osm-polygon-description-tag",
            preflight=_fake_preflight,
            upload_runner=fail_on_b,
            clock=_frozen_clock,
            exporter=exporter,
            verifier=lambda repo_id, files: "verified-rev",
        )

    # State should record "a.osm.pbf" as published; "b" should still be missing.
    state = read_publication_state(data_root)
    assert "a.osm.pbf" in state["published"]
    assert "b.osm.pbf" not in state["published"]

    # Restart: succeed for both. "a" should be skipped; "b" should be uploaded.
    def succeed_all(command: list[str]) -> str:
        for inc in command:
            if inc.startswith("data/"):
                uploaded.append(inc)
        if "data/a.parquet" in command:
            return "r-a-restart"
        return "r-b-restart"

    report = run_and_publish(
        paths=paths,
        confirm_repo="NoeFlandre/osm-polygon-description-tag",
        preflight=_fake_preflight,
        upload_runner=succeed_all,
        clock=_frozen_clock,
        exporter=exporter,
        verifier=lambda repo_id, files: "verified-rev",
    )

    state = read_publication_state(data_root)
    assert "a.osm.pbf" in state["published"]
    assert "b.osm.pbf" in state["published"]
    # Verify both parquets exist on disk.
    assert (data_root / "data" / "a.parquet").is_file()
    assert (data_root / "data" / "b.parquet").is_file()
    assert (data_root / PUBLICATION_STATE_FILENAME).is_file()
    assert report.final_remote_revision == "verified-rev"


def test_run_and_publish_safe_upload_retry_after_interrupt(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """An upload whose remote commit succeeded but local checkpoint failed is safely retried."""
    source_root = tmp_path / "raw"
    data_root = tmp_path / "generated"
    source_root.mkdir()
    data_root.mkdir()
    (source_root / "a.osm.pbf").write_bytes(b"a-bytes")
    paths = Paths(source_root=source_root, data_root=data_root)

    exporter = _fake_exporter_factory({"a": 1})

    # First upload: succeeds but we patch _write_publication_state to silently
    # fail (simulate crash after remote commit but before local checkpoint).
    from osm_polygon_description_tag import orchestrator

    real_write = orchestrator._write_publication_state

    def flaky_write(*args, **kwargs):
        if not hasattr(flaky_write, "called"):
            flaky_write.called = True  # type: ignore[attr-defined]
            raise RuntimeError("simulated crash after remote commit")
        return real_write(*args, **kwargs)

    monkeypatch.setattr(orchestrator, "_write_publication_state", flaky_write)

    def upload_succeeds(command: list[str]) -> str:
        return "r-a"

    with pytest.raises(RuntimeError, match="simulated crash"):
        run_and_publish(
            paths=paths,
            confirm_repo="NoeFlandre/osm-polygon-description-tag",
            preflight=_fake_preflight,
            upload_runner=upload_succeeds,
            clock=_frozen_clock,
            exporter=exporter,
            verifier=lambda repo_id, files: "verified-rev",
        )

    # After simulated crash, the local publication state has not been updated.
    state = read_publication_state(data_root)
    assert "a.osm.pbf" not in state["published"]

    # Restart: now the original write succeeds, so state records the upload.
    monkeypatch.setattr(orchestrator, "_write_publication_state", real_write)
    report = run_and_publish(
        paths=paths,
        confirm_repo="NoeFlandre/osm-polygon-description-tag",
        preflight=_fake_preflight,
        upload_runner=upload_succeeds,
        clock=_frozen_clock,
        exporter=exporter,
        verifier=lambda repo_id, files: "verified-rev",
    )
    state = read_publication_state(data_root)
    assert "a.osm.pbf" in state["published"]
    assert report.final_remote_revision == "verified-rev"


def test_run_and_publish_handles_interrupt_before_publish_state(
    tmp_path: Path,
) -> None:
    """An interrupt after build but before checkpoint leaves no publication state.

    The parquet exists (built), the manifest exists (written by build_one),
    but the publication state has not been updated.
    """
    source_root = tmp_path / "raw"
    data_root = tmp_path / "generated"
    source_root.mkdir()
    data_root.mkdir()
    (source_root / "a.osm.pbf").write_bytes(b"a-bytes")
    paths = Paths(source_root=source_root, data_root=data_root)

    def interrupted_upload(command: list[str]) -> str:
        raise KeyboardInterrupt()

    with pytest.raises(KeyboardInterrupt):
        run_and_publish(
            paths=paths,
            confirm_repo="NoeFlandre/osm-polygon-description-tag",
            preflight=_fake_preflight,
            upload_runner=interrupted_upload,
            clock=_frozen_clock,
            exporter=_fake_exporter_factory({"a": 1}),
            verifier=lambda repo_id, files: "verified-rev",
        )

    # Parquet and manifest were created by build_one, but publication state is empty.
    assert (data_root / "data" / "a.parquet").exists()
    assert (data_root / "manifests" / "a.manifest.json").exists()
    assert not (data_root / PUBLICATION_STATE_FILENAME).exists()
