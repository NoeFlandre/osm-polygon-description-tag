"""Hermetic 3-run synthetic integration test covering the H3 map lifecycle.

This test exercises the orchestrator's stoppable, resumable publication
end-to-end across three runs, with a focus on the H3 density map:

* Run 1 builds a first PBF, regenerates the H3 map, and is interrupted
  while the second PBF's upload is in flight.
* Run 2 reuses the first PBF, regenerates/validates the map, completes
  publication, and uploads the final metadata plan including the map.
* Run 3 is a true no-op: every artifact and the publication state are
  byte-for-byte preserved, and zero uploads occur.

The test fakes only:

* the Hugging Face Hub API (``HfApi`` on the workflow's lazy wrapper);
* the ``hf upload-large-folder`` subprocess (injected via the
  ``upload_runner`` hook);
* the ``HubVerifier`` (injected via the ``verifier`` parameter).

The test never reads real PBFs and never contacts the live Hub.
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pytest
from shapely import to_wkb
from shapely.geometry import Polygon

from osm_polygon_description_tag.config import Paths
from osm_polygon_description_tag.extraction import ExportRecord
from osm_polygon_description_tag.orchestrator import (
    PUBLICATION_STATE_FILENAME,
    run_and_publish,
)
from osm_polygon_description_tag.publication import REPO_ID

_CLOCK = "2026-07-30T00:00:00+00:00"


def _frozen_clock() -> str:
    return _CLOCK


def _file_sha(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _fake_exporter_factory() -> object:
    def _export(source_path: Path, _cfg: Path) -> object:
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
            tags=json.loads('{"description": "x"}'),
        )
        return iter([record])

    return _export


def _setup_two_sources(tmp_path: Path) -> tuple[Paths, Path, Path]:
    source_root = tmp_path / "raw"
    data_root = tmp_path / "generated"
    source_root.mkdir()
    data_root.mkdir()
    (source_root / "alpha.osm.pbf").write_bytes(b"alpha-bytes")
    (source_root / "beta.osm.pbf").write_bytes(b"beta-bytes")
    return Paths(source_root=source_root, data_root=data_root), source_root, data_root


def _install_hf_stubs(
    monkeypatch: pytest.MonkeyPatch, *, interrupted_run: int | None = None
) -> dict:
    """Install HermeticHF stubs and return a per-run log."""
    import osm_polygon_description_tag.osm.extraction as extraction_module
    import osm_polygon_description_tag.publication.upload as pub
    import osm_polygon_description_tag.runtime.resources as resources_module
    import osm_polygon_description_tag.workflow.orchestrator as orch
    import osm_polygon_description_tag.workflow.preflight as preflight_module

    log: dict[str, object] = {
        "uploads": 0,
        "verifier_calls": 0,
        "interrupted": False,
        "per_pbf_calls": 0,
        "metadata_calls": 0,
    }

    def runner_wrapper(command: list[str], timeout: float | None = None) -> None:
        includes = [
            command[index + 1] for index, piece in enumerate(command) if piece == "--include"
        ]
        log["uploads"] = int(log["uploads"]) + 1
        if any(item.startswith("data/") for item in includes):
            log["per_pbf_calls"] = int(log["per_pbf_calls"]) + 1
            if interrupted_run is not None and log["per_pbf_calls"] == interrupted_run:
                log["interrupted"] = True
                raise KeyboardInterrupt
        else:
            log["metadata_calls"] = int(log["metadata_calls"]) + 1

    def hfapi_factory() -> object:
        class _Stub:
            def whoami(self) -> object:
                return {"name": "fake"}

            def repo_info(self, *_a: object, **_kw: object) -> object:
                class _Info:
                    sha = "abc"

                return _Info()

            def auth_check(self, *_a: object, **_kw: object) -> None:
                return None

        return _Stub()

    def verifier(repo_id: str, files: tuple[object, ...]) -> str:
        log["verifier_calls"] = int(log["verifier_calls"]) + 1
        return f"hub-rev-{log['verifier_calls']}"

    def preflight_runner(command: list[str], text: bool = False, **_kwargs: object) -> object:  # type: ignore[name-defined]
        import subprocess

        def _response(stdout: str | bytes) -> object:
            if text:
                return subprocess.CompletedProcess(
                    command,
                    returncode=0,
                    stdout=stdout if isinstance(stdout, str) else stdout.decode("utf-8"),
                    stderr="" if text else b"",
                )
            return subprocess.CompletedProcess(
                command,
                returncode=0,
                stdout=stdout if isinstance(stdout, bytes) else stdout.encode("utf-8"),
                stderr="" if text else b"",
            )

        if command == ["osmium", "--version"]:
            return _response("osmium version 1.19.1\n")
        if command == ["hf", "auth", "whoami"]:
            return _response("fake-user\n")
        if len(command) >= 3 and command[0] == "git" and "rev-parse" in command:
            return _response("abc123\n")
        raise AssertionError(f"unexpected preflight subprocess: {command!r}")

    monkeypatch.setattr(extraction_module.subprocess, "run", preflight_runner)
    monkeypatch.setattr(resources_module.subprocess, "run", preflight_runner)
    monkeypatch.setattr(pub, "_default_runner_with_retry", runner_wrapper)
    monkeypatch.setattr(orch, "default_hub_verifier_factory", lambda: verifier)
    monkeypatch.setattr(orch, "_default_clock", lambda: "2026-01-01T00:00:00+00:00")
    monkeypatch.setattr(preflight_module._huggingface_hub, "HfApi", hfapi_factory)
    monkeypatch.setattr(preflight_module.subprocess, "run", preflight_runner)
    monkeypatch.setattr(preflight_module.shutil, "which", lambda executable: executable)
    return log


def _stub_h3_render(monkeypatch: pytest.MonkeyPatch, data_root: Path) -> None:
    """Stub the H3 map rendering to write a deterministic PNG.

    The deterministic PNG is enough to exercise publication-state
    identity and plan inclusion without spinning up matplotlib.
    """
    from PIL import Image

    def _write_png(data_root_arg: Path, total_rows: int, occupied_cells: int) -> None:
        assets_dir = data_root_arg / "assets"
        assets_dir.mkdir(parents=True, exist_ok=True)
        target = assets_dir / "description_polygon_density.png"
        Image.new("RGB", (16, 8), (255, 255, 255)).save(target)

    monkeypatch.setattr(
        "osm_polygon_description_tag.dataset.reporting._write_h3_map_png",
        _write_png,
        raising=False,
    )


def test_three_run_map_lifecycle(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    paths, source_root, data_root = _setup_two_sources(tmp_path)
    _stub_h3_render(monkeypatch, data_root)

    # -------------------------------------------------------------------------
    # Run 1: build alpha, upload alpha, build beta, interrupt during beta's upload.
    # -------------------------------------------------------------------------
    log_run_1 = _install_hf_stubs(monkeypatch, interrupted_run=2)
    with pytest.raises(KeyboardInterrupt):
        run_and_publish(
            paths=paths,
            confirm_repo=REPO_ID,
            preflight=lambda: {"preflight": "stub", "source_count": 2},
            clock=_frozen_clock,
            exporter=_fake_exporter_factory(),
        )
    assert log_run_1["interrupted"] is True
    # Alpha is fully published (build + verify + state).
    state = json.loads((data_root / PUBLICATION_STATE_FILENAME).read_text(encoding="utf-8"))
    assert "alpha.osm.pbf" in state["published"]
    assert "beta.osm.pbf" not in state["published"]
    # The map is present.
    map_path = data_root / "assets" / "description_polygon_density.png"
    assert map_path.is_file()

    snapshot_alpha_parquet = _file_sha(data_root / "data" / "alpha.parquet")
    snapshot_alpha_manifest = _file_sha(data_root / "manifests" / "alpha.manifest.json")
    snapshot_map = _file_sha(map_path)

    # -------------------------------------------------------------------------
    # Run 2: complete beta and publish the final metadata plan.
    # -------------------------------------------------------------------------
    _install_hf_stubs(monkeypatch, interrupted_run=None)
    run_and_publish(
        paths=paths,
        confirm_repo=REPO_ID,
        preflight=lambda: {"preflight": "stub", "source_count": 2},
        clock=_frozen_clock,
        exporter=_fake_exporter_factory(),
    )
    state = json.loads((data_root / PUBLICATION_STATE_FILENAME).read_text(encoding="utf-8"))
    assert "alpha.osm.pbf" in state["published"]
    assert "beta.osm.pbf" in state["published"]
    assert "metadata" in state
    # Alpha is reused: its parquet/manifest must not change.
    assert _file_sha(data_root / "data" / "alpha.parquet") == snapshot_alpha_parquet
    assert _file_sha(data_root / "manifests" / "alpha.manifest.json") == snapshot_alpha_manifest
    # The map is recorded in the metadata state.
    metadata = state["metadata"]
    assert metadata["h3_map_sha256"] == snapshot_map
    assert metadata["h3_map_size_bytes"] == map_path.stat().st_size

    snapshot_state = _file_sha(data_root / PUBLICATION_STATE_FILENAME)
    snapshot_readme = _file_sha(data_root / "README.md")
    snapshot_stats = _file_sha(data_root / "stats.json")

    # -------------------------------------------------------------------------
    # Run 3: pure no-op. Zero uploads, zero verifier calls.
    # -------------------------------------------------------------------------
    log_run_3 = _install_hf_stubs(monkeypatch, interrupted_run=None)
    report = run_and_publish(
        paths=paths,
        confirm_repo=REPO_ID,
        preflight=lambda: {"preflight": "stub", "source_count": 2},
        clock=_frozen_clock,
        exporter=_fake_exporter_factory(),
    )
    assert log_run_3["uploads"] == 0
    assert log_run_3["verifier_calls"] == 0
    for outcome in report.outcomes:
        assert outcome.status == "already-published"
    # Every artifact is byte-for-byte preserved.
    assert _file_sha(data_root / PUBLICATION_STATE_FILENAME) == snapshot_state
    assert _file_sha(data_root / "README.md") == snapshot_readme
    assert _file_sha(data_root / "stats.json") == snapshot_stats
    assert _file_sha(data_root / "data" / "alpha.parquet") == snapshot_alpha_parquet
    assert _file_sha(data_root / "manifests" / "alpha.manifest.json") == snapshot_alpha_manifest
    assert _file_sha(map_path) == snapshot_map
