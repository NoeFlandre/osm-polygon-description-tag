"""Hermetic integration test for the H3 map migration contract.

This test exercises the orchestrator's behavior when ``run_and_publish``
encounters a dataset that was published BEFORE the H3 map feature was
introduced:

* the publication state marks every source as already-published;
* ``README.md`` is the pre-feature card (no H3 map marker block);
* ``assets/description_polygon_density.png`` does not exist;
* no per-PBF upload has run yet because everything was already published;
* the metadata-plan upload (README, stats, map) is the only thing that
  should happen in this run.

A second run on the same workspace must be a true no-op: zero uploads,
zero verifier calls, every artifact byte-identical.
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pytest
from shapely.geometry import Polygon

from osm_polygon_description_tag._resources import dataset_card_template, project_code_revision
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
from osm_polygon_description_tag.orchestrator import (
    PUBLICATION_STATE_FILENAME,
    run_and_publish,
)
from osm_polygon_description_tag.publication import REPO_ID
from tests.conftest import make_record_dict

_CLOCK = "2026-07-30T00:00:00+00:00"


def _frozen_clock() -> str:
    return _CLOCK


def _file_sha(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _setup_pre_h3_dataset(tmp_path: Path) -> tuple[Paths, Path, Path]:
    """Build a fully-published pre-feature dataset layout.

    The README is the pre-feature template (no H3 marker); the assets/
    directory is missing; every source has its parquet, manifest, and a
    matching publication-state entry.
    """
    source_root = tmp_path / "raw"
    data_root = tmp_path / "generated"
    source_root.mkdir()
    data_root.mkdir()
    (source_root / "alpha.osm.pbf").write_bytes(b"alpha-bytes")
    (source_root / "beta.osm.pbf").write_bytes(b"beta-bytes")

    written: dict[str, dict[str, str | int]] = {}
    for stem in ("alpha", "beta"):
        source_name = f"{stem}.osm.pbf"
        record = make_record_dict(
            Polygon([(0, 0), (0, 1), (1, 1), (1, 0)]),
            {"description": stem},
            osm_id=abs(hash(stem)) % 1000000,
            source_pbf=source_name,
        )
        output = data_root / "data" / f"{stem}.parquet"
        (data_root / "data").mkdir(exist_ok=True)
        write_geoparquet(iter([record]), output, batch_size=10)
        manifest = Manifest(
            manifest_schema_version=2,
            schema_version=2,
            geoparquet_version="1.1.0",
            transform_algorithm_version=2,
            area_policy_sha256=current_area_policy_sha256(),
            output_algorithm_revision=current_output_algorithm_revision(),
            source=source_identity_for(source_root / source_name),
            output=output_identity_for(output),
            osmium_version="osmium version 1.19.1",
            dependency_versions={"pyarrow": "20.0.0"},
            code_revision=project_code_revision(),
            started_at=_CLOCK,
            completed_at=_CLOCK,
            counts=RunCounts(emitted_features=1, included_rows=1, rejections={}),
        )
        (data_root / "manifests").mkdir(exist_ok=True)
        write_manifest(manifest, data_root / "manifests" / f"{stem}.manifest.json")
        written[source_name] = {
            "source_sha256": manifest.source.sha256,
            "output_sha256": manifest.output.sha256,
            "output_bytes": output.stat().st_size,
            "rev": f"pre-rev-{stem}",
        }

    # Pre-feature README: stats block empty, NO H3 marker block.
    template_path = dataset_card_template()
    pre_h3_text = template_path.read_text(encoding="utf-8")
    assert "GENERATED:H3_MAP" in pre_h3_text  # current template DOES carry it

    # Strip the current template's H3 marker block to produce a pre-feature
    # README that EXACTLY matches the pre-feature template bytes.
    import re

    pre_h3_block = re.compile(
        r"<!-- GENERATED:H3_MAP:START -->[^\n]*\n.*?<!-- GENERATED:H3_MAP:END -->\n",
        re.DOTALL,
    )
    pre_feature_template = pre_h3_block.sub("", pre_h3_text, count=1)
    (data_root / "README.md").write_text(pre_feature_template, encoding="utf-8")

    # The orchestrator must install the H3 marker block during this run.
    assert "GENERATED:H3_MAP" not in (data_root / "README.md").read_text(encoding="utf-8")

    # Pre-publish every source.
    state = {
        "schema_version": 1,
        "published": {
            name: {
                "source_sha256": data["source_sha256"],
                "output_sha256": data["output_sha256"],
                "output_bytes": data["output_bytes"],
                "remote_revision": data["rev"],
                "artifact_identity": "00" * 32,
                "completed_at": _CLOCK,
            }
            for name, data in written.items()
        },
    }
    (data_root / PUBLICATION_STATE_FILENAME).write_text(
        json.dumps(state, sort_keys=True, indent=2), encoding="utf-8"
    )
    return Paths(source_root=source_root, data_root=data_root), source_root, data_root


def _stub_png_render(monkeypatch: pytest.MonkeyPatch, data_root: Path) -> None:
    """Stub matplotlib to a no-op so the test does not pull in matplotlib's image backend."""

    def _stub_write_h3_map_png(data_root_arg: Path, total_rows: int, occupied_cells: int) -> None:
        from PIL import Image

        target = data_root_arg / "assets" / "description_polygon_density.png"
        target.parent.mkdir(parents=True, exist_ok=True)
        # Tiny deterministic PNG; the bytes feed directly into the
        # publication-state identity check.
        Image.new("RGB", (16, 8), (255, 255, 255)).save(target)

    monkeypatch.setattr(
        "osm_polygon_description_tag.dataset.reporting._write_h3_map_png",
        _stub_write_h3_map_png,
        raising=False,
    )


def _install_external_boundaries(monkeypatch: pytest.MonkeyPatch) -> dict:
    import osm_polygon_description_tag.osm.extraction as extraction_module
    import osm_polygon_description_tag.publication.upload as pub
    import osm_polygon_description_tag.runtime.resources as resources_module
    import osm_polygon_description_tag.workflow.orchestrator as orch
    import osm_polygon_description_tag.workflow.preflight as preflight_module

    log: dict[str, object] = {
        "uploads": 0,
        "verifier_calls": 0,
        "per_pbf_calls": 0,
        "metadata_calls": 0,
    }

    def runner_wrapper(command: list[str], timeout: float | None = None) -> str:
        includes = [
            command[index + 1] for index, piece in enumerate(command) if piece == "--include"
        ]
        log["uploads"] = int(log["uploads"]) + 1
        if any(item.startswith("data/") for item in includes):
            log["per_pbf_calls"] = int(log["per_pbf_calls"]) + 1
        else:
            log["metadata_calls"] = int(log["metadata_calls"]) + 1
        return f"rev-{log['uploads']}"

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

    class _Stub:
        def whoami(self) -> object:
            return {"name": "fake"}

        def repo_info(self, *_a: object, **_kw: object) -> object:
            class _Info:
                sha = "abc"

            return _Info()

        def auth_check(self, *_a: object, **_kw: object) -> None:
            return None

    monkeypatch.setattr(extraction_module.subprocess, "run", preflight_runner)
    monkeypatch.setattr(resources_module.subprocess, "run", preflight_runner)
    monkeypatch.setattr(preflight_module.subprocess, "run", preflight_runner)
    monkeypatch.setattr(preflight_module._huggingface_hub, "HfApi", lambda *a, **k: _Stub())
    monkeypatch.setattr(preflight_module.shutil, "which", lambda executable: executable)
    monkeypatch.setattr(pub, "_default_runner_with_retry", runner_wrapper)
    monkeypatch.setattr(orch, "default_hub_verifier_factory", lambda: verifier)
    monkeypatch.setattr(orch, "_default_clock", lambda: _CLOCK)
    return log


def test_pre_h3_migration_refreshes_readme_writes_map_and_uploads_metadata(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A pre-feature dataset must be repaired in a single run with one metadata upload."""
    paths, _source_root, data_root = _setup_pre_h3_dataset(tmp_path)
    _stub_png_render(monkeypatch, data_root)
    log = _install_external_boundaries(monkeypatch)

    assert not (data_root / "assets").exists()
    assert "GENERATED:H3_MAP:START" not in (data_root / "README.md").read_text(encoding="utf-8")

    report = run_and_publish(
        paths=paths,
        confirm_repo=REPO_ID,
        preflight=lambda: {"preflight": "stub", "source_count": 2},
        clock=_frozen_clock,
    )

    # Every source is already-published; no per-PBF upload occurred.
    for outcome in report.outcomes:
        assert outcome.status == "already-published"
    assert log["per_pbf_calls"] == 0

    # Exactly one metadata upload (README + stats + map).
    assert log["metadata_calls"] == 1
    assert log["uploads"] == 1
    assert log["verifier_calls"] == 1

    # The orchestrator installed the H3 marker block and rendered the PNG.
    readme = (data_root / "README.md").read_text(encoding="utf-8")
    assert "<!-- GENERATED:H3_MAP:START -->" in readme
    assert "H3 density of description-tagged polygons" in readme
    map_path = data_root / "assets" / "description_polygon_density.png"
    assert map_path.is_file()

    # Publication state records the map identity.
    state = json.loads((data_root / PUBLICATION_STATE_FILENAME).read_text(encoding="utf-8"))
    metadata = state["metadata"]
    assert metadata["h3_map_sha256"] == file_sha256(map_path)
    assert metadata["h3_map_size_bytes"] == map_path.stat().st_size

    snapshot_state = _file_sha(data_root / PUBLICATION_STATE_FILENAME)
    snapshot_readme = _file_sha(data_root / "README.md")
    snapshot_stats = _file_sha(data_root / "stats.json")
    snapshot_map = _file_sha(map_path)

    # Second run must be a true no-op: zero activity, every byte preserved.
    log2 = _install_external_boundaries(monkeypatch)
    report_2 = run_and_publish(
        paths=paths,
        confirm_repo=REPO_ID,
        preflight=lambda: {"preflight": "stub", "source_count": 2},
        clock=_frozen_clock,
    )
    for outcome in report_2.outcomes:
        assert outcome.status == "already-published"
    assert log2["uploads"] == 0
    assert log2["verifier_calls"] == 0
    assert _file_sha(data_root / PUBLICATION_STATE_FILENAME) == snapshot_state
    assert _file_sha(data_root / "README.md") == snapshot_readme
    assert _file_sha(data_root / "stats.json") == snapshot_stats
    assert _file_sha(map_path) == snapshot_map


def test_orchestrator_repairs_stale_readme_before_metadata_publish(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Orchestrator repairs a stale README before publishing metadata."""
    paths, _source_root, data_root = _setup_pre_h3_dataset(tmp_path)
    _stub_png_render(monkeypatch, data_root)
    log = _install_external_boundaries(monkeypatch)

    # Plant a stale README that already has the H3 marker block but with
    # wrong content, plus a fresh state that is otherwise complete. This
    # simulates a partial migration where the asset directory exists but
    # the README card is out of date.
    (data_root / "assets").mkdir()
    (data_root / "assets" / "description_polygon_density.png").write_bytes(
        b"\x89PNG\r\n\x1a\n" + b"old" * 1024
    )
    # Mutate the README: remove the H3 marker block so it's still
    # missing.
    import re

    text = (data_root / "README.md").read_text(encoding="utf-8")
    readme_no_h3 = re.compile(
        r"<!-- GENERATED:H3_MAP:START -->.*?<!-- GENERATED:H3_MAP:END -->\n",
        re.DOTALL,
    )
    readme_no_h3_text = readme_no_h3.sub("", text, count=1)
    (data_root / "README.md").write_text(readme_no_h3_text, encoding="utf-8")

    snapshot_pre_readme = _file_sha(data_root / "README.md")
    snapshot_pre_map = _file_sha(data_root / "assets" / "description_polygon_density.png")

    report = run_and_publish(
        paths=paths,
        confirm_repo=REPO_ID,
        preflight=lambda: {"preflight": "stub", "source_count": 2},
        clock=_frozen_clock,
    )
    # Repaired before publication: README is regenerated, map is rewritten.
    for outcome in report.outcomes:
        assert outcome.status == "already-published"
    assert log["metadata_calls"] == 1
    assert log["per_pbf_calls"] == 0
    new_readme_sha = _file_sha(data_root / "README.md")
    new_map_sha = _file_sha(data_root / "assets" / "description_polygon_density.png")
    # The README was regenerated (and the map bytes differ from the
    # planted stale bytes).
    assert new_readme_sha != snapshot_pre_readme
    assert new_map_sha != snapshot_pre_map
    assert "<!-- GENERATED:H3_MAP:START -->" in (data_root / "README.md").read_text(
        encoding="utf-8"
    )
