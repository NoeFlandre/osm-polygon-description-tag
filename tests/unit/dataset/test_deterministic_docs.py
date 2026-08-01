"""Deterministic documentation contract for the amendment dataset.

The generated ``stats.json`` and dataset card must be byte-stable across
regenerations with different wall clocks, as long as the input
artifacts, the card template, and the published validation outputs are
unchanged. Identical regeneration must not invalidate the metadata
publication state and must not cause an additional metadata upload.
"""

from __future__ import annotations

import hashlib
import json
import subprocess
from pathlib import Path

import pytest
from shapely.geometry import Polygon

from osm_polygon_description_tag._resources import dataset_card_template
from osm_polygon_description_tag.cli import run as cli_run
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
from osm_polygon_description_tag.dataset.reporting import (
    _render_stats_block,
    collect_stats,
    generate_dataset_docs,
)
from osm_polygon_description_tag.dataset.storage import write_geoparquet
from osm_polygon_description_tag.publication import REPO_ID
from tests.conftest import make_record_dict


def _sha256_text(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _populate_dataset(data_root: Path, source_root: Path) -> None:
    (data_root / "data").mkdir(parents=True)
    (data_root / "manifests").mkdir(parents=True)
    source_root.mkdir(exist_ok=True)
    for stem, (osm_id, tags) in {
        "alpha": (1, {"name": "Alpha", "description": "First", "name:en": "EN"}),
        "beta": (2, {"name:fr": "Beta FR", "description": "Second"}),
    }.items():
        source = source_root / f"{stem}.osm.pbf"
        source.write_bytes(stem.encode("utf-8"))
        record = make_record_dict(
            Polygon([(0, 0), (0, 1), (1, 1), (1, 0)]),
            tags,
            osm_id=osm_id,
            source_pbf=source.name,
        )
        output = data_root / "data" / f"{stem}.parquet"
        write_geoparquet(iter([record]), output, batch_size=10)
        write_manifest(
            Manifest(
                manifest_schema_version=2,
                schema_version=2,
                geoparquet_version="1.1.0",
                transform_algorithm_version=2,
                area_policy_sha256=current_area_policy_sha256(),
                output_algorithm_revision=current_output_algorithm_revision(),
                source=source_identity_for(source),
                output=output_identity_for(output),
                osmium_version=None,
                dependency_versions={"pyarrow": "20.0.0"},
                code_revision=None,
                started_at="2026-01-01T00:00:00+00:00",
                completed_at="2026-01-01T00:00:01+00:00",
                counts=RunCounts(emitted_features=1, included_rows=1, rejections={}),
            ),
            data_root / "manifests" / f"{stem}.manifest.json",
        )


def test_generation_is_byte_stable_across_clocks(tmp_path: Path) -> None:
    """Two generations with different clocks produce byte-identical outputs."""
    data_root = tmp_path / "generated"
    source_root = tmp_path / "raw"
    _populate_dataset(data_root, source_root)
    template = dataset_card_template()

    generate_dataset_docs(data_root, template, clock=lambda: "2026-01-01T00:00:00+00:00")
    readme_a = (data_root / "README.md").read_text(encoding="utf-8")
    stats_a = (data_root / "stats.json").read_text(encoding="utf-8")

    generate_dataset_docs(data_root, template, clock=lambda: "2099-12-31T23:59:59+00:00")
    readme_b = (data_root / "README.md").read_text(encoding="utf-8")
    stats_b = (data_root / "stats.json").read_text(encoding="utf-8")

    assert readme_a == readme_b
    assert stats_a == stats_b
    assert _sha256_text(readme_a) == _sha256_text(readme_b)
    assert _sha256_text(stats_a) == _sha256_text(stats_b)


def test_generation_does_not_contain_wall_clock_field(tmp_path: Path) -> None:
    """Generated stats must not contain a wall-clock value."""
    data_root = tmp_path / "generated"
    source_root = tmp_path / "raw"
    _populate_dataset(data_root, source_root)
    generate_dataset_docs(data_root, dataset_card_template())
    stats = json.loads((data_root / "stats.json").read_text(encoding="utf-8"))
    forbidden = {"generation_timestamp_utc", "generation_timestamp", "now", "wall_clock"}
    assert forbidden.isdisjoint(stats.keys())
    readme = (data_root / "README.md").read_text(encoding="utf-8")
    assert "Generated at" not in readme
    # Dataset-derived timestamps (e.g. OSM data min/max) are still allowed.
    assert "wall_clock" not in readme.lower()
    assert "2099-12-31" not in readme


def test_identical_regeneration_preserves_mtimes(tmp_path: Path) -> None:
    """A second regeneration over unchanged inputs does not modify mtimes."""
    data_root = tmp_path / "generated"
    source_root = tmp_path / "raw"
    _populate_dataset(data_root, source_root)
    generate_dataset_docs(data_root, dataset_card_template())
    readme_mtime = (data_root / "README.md").stat().st_mtime_ns
    stats_mtime = (data_root / "stats.json").stat().st_mtime_ns
    import time

    time.sleep(0.05)
    generate_dataset_docs(data_root, dataset_card_template())
    assert (data_root / "README.md").stat().st_mtime_ns == readme_mtime
    assert (data_root / "stats.json").stat().st_mtime_ns == stats_mtime


def test_stats_block_includes_name_localized_and_suffixes(tmp_path: Path) -> None:
    """The deterministic stats block records exact name presence and suffixes."""
    data_root = tmp_path / "generated"
    source_root = tmp_path / "raw"
    _populate_dataset(data_root, source_root)
    stats = collect_stats(data_root)
    assert stats["base_name_rows"] == 1
    assert stats["localized_name_rows"] == 2
    assert stats["name_suffixes"] == {"en": 1, "fr": 1}
    assert stats["output_files"] == 2
    assert stats["rows"] == 2


def test_stats_payload_retains_deterministic_per_file_provenance(tmp_path: Path) -> None:
    """Detailed per-file provenance remains available in stats.json."""
    data_root = tmp_path / "generated"
    source_root = tmp_path / "raw"
    _populate_dataset(data_root, source_root)
    stats = collect_stats(data_root)
    files = stats["files"]
    names = sorted(f["parquet"] for f in files)
    assert names == ["alpha.parquet", "beta.parquet"]
    for entry in files:
        assert set(entry) == {
            "source_pbf",
            "parquet",
            "rows",
            "source_bytes",
            "output_bytes",
            "emitted_features",
            "rejections",
            "source_sha256",
            "output_sha256",
        }
        assert len(entry["source_sha256"]) == 64
        assert len(entry["output_sha256"]) == 64
        assert entry["output_bytes"] > 0
        assert entry["source_bytes"] > 0


def test_card_renders_only_ten_suffixes_in_deterministic_order(tmp_path: Path) -> None:
    data_root = tmp_path / "generated"
    source_root = tmp_path / "raw"
    _populate_dataset(data_root, source_root)
    stats = collect_stats(data_root)
    stats["description_suffixes"] = {f"s{index:02d}": 1 for index in range(11)}

    rendered = _render_stats_block(stats, "0" * 64)

    positions = [rendered.index(f"| `s{index:02d}` |") for index in range(10)]
    assert positions == sorted(positions)
    assert "| `s10` |" not in rendered


def test_identical_regeneration_does_not_invalidate_metadata_state(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Identical regeneration of docs does not require a fresh metadata upload."""
    paths, source_root, data_root = _setup_workspace(tmp_path)
    _plant_resumable(data_root, source_root, "a.osm.pbf")
    _plant_resumable(data_root, source_root, "b.osm.pbf")
    _plant_metadata(data_root)
    _publish_full_run(monkeypatch, source_root, data_root)

    from osm_polygon_description_tag.orchestrator import PUBLICATION_STATE_FILENAME
    from osm_polygon_description_tag.publication import _build_metadata_only_upload_plan

    plan = _build_metadata_only_upload_plan(data_root)
    state_path = data_root / PUBLICATION_STATE_FILENAME
    state = json.loads(state_path.read_text(encoding="utf-8"))
    assert state["metadata"]["identity_sha256"] == plan.identity_sha256

    snapshot_state = file_sha256(state_path)
    snapshot_readme = file_sha256(data_root / "README.md")
    snapshot_stats = file_sha256(data_root / "stats.json")

    log = _install_subprocess_recorder(monkeypatch, action="noop")
    exit_code = cli_run(
        [
            "run-and-publish",
            "--source-root",
            str(source_root),
            "--data-root",
            str(data_root),
            "--confirm-repo",
            REPO_ID,
        ]
    )
    assert exit_code == 0
    assert log["preflight_commands"] == [
        ("osmium", "--version"),
        ("hf", "auth", "whoami"),
    ]
    assert log["uploads"] == 0
    assert log["verifier_calls"] == 0
    assert file_sha256(state_path) == snapshot_state
    assert file_sha256(data_root / "README.md") == snapshot_readme
    assert file_sha256(data_root / "stats.json") == snapshot_stats


def _setup_workspace(tmp_path: Path) -> tuple[Paths, Path, Path]:
    source_root = tmp_path / "raw"
    data_root = tmp_path / "generated"
    source_root.mkdir()
    data_root.mkdir()
    (source_root / "a.osm.pbf").write_bytes(b"a-bytes")
    (source_root / "b.osm.pbf").write_bytes(b"b-bytes")
    return Paths(source_root=source_root, data_root=data_root), source_root, data_root


def _plant_resumable(data_root: Path, source_root: Path, source_name: str) -> None:
    stem = source_name.removesuffix(".osm.pbf")
    (data_root / "data").mkdir(parents=True, exist_ok=True)
    (data_root / "manifests").mkdir(parents=True, exist_ok=True)
    record = make_record_dict(
        Polygon([(0, 0), (0, 1), (1, 1), (1, 0)]),
        {"description": "x", "name": "X"},
        osm_id=1,
        source_pbf=source_name,
    )
    write_geoparquet(iter([record]), data_root / "data" / f"{stem}.parquet", batch_size=10)
    source_path = source_root / source_name
    write_manifest(
        Manifest(
            manifest_schema_version=2,
            schema_version=2,
            geoparquet_version="1.1.0",
            transform_algorithm_version=2,
            area_policy_sha256=current_area_policy_sha256(),
            output_algorithm_revision=current_output_algorithm_revision(),
            source=source_identity_for(source_path),
            output=output_identity_for(data_root / "data" / f"{stem}.parquet"),
            osmium_version=None,
            dependency_versions={"pyarrow": "20.0.0"},
            code_revision=None,
            started_at="2026-01-01T00:00:00+00:00",
            completed_at="2026-01-01T00:00:01+00:00",
            counts=RunCounts(emitted_features=1, included_rows=1, rejections={}),
        ),
        data_root / "manifests" / f"{stem}.manifest.json",
    )


def _plant_metadata(data_root: Path) -> None:
    (data_root / "README.md").write_text("# README")
    (data_root / "stats.json").write_text("{}")


def _install_subprocess_recorder(monkeypatch: pytest.MonkeyPatch, *, action: str = "ok") -> dict:
    import osm_polygon_description_tag.publication.upload as pub
    import osm_polygon_description_tag.workflow.orchestrator as orch
    import osm_polygon_description_tag.workflow.preflight as preflight_module

    log = {"uploads": 0, "verifier_calls": 0, "preflight_commands": []}

    def preflight_runner(
        command: list[str],
        **_kwargs: object,
    ) -> subprocess.CompletedProcess[str]:
        log["preflight_commands"].append(tuple(command))
        if command == ["osmium", "--version"]:
            return subprocess.CompletedProcess(
                command,
                returncode=0,
                stdout="osmium version 1.19.1\n",
                stderr="",
            )
        if command == ["hf", "auth", "whoami"]:
            return subprocess.CompletedProcess(
                command,
                returncode=0,
                stdout="fake-user\n",
                stderr="",
            )
        raise AssertionError(f"unexpected preflight subprocess: {command!r}")

    def runner(command: list[str], timeout: float | None = None) -> None:
        log["uploads"] += 1

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

    def verifier_factory():
        def f(_repo_id: str, _files: object) -> str:
            log["verifier_calls"] += 1
            return "rev"

        return f

    monkeypatch.setattr(pub, "_default_runner_with_retry", runner)
    monkeypatch.setattr(preflight_module.subprocess, "run", preflight_runner)
    monkeypatch.setattr(preflight_module.shutil, "which", lambda executable: executable)
    monkeypatch.setattr(orch, "default_hub_verifier_factory", verifier_factory)
    monkeypatch.setattr(orch, "_default_clock", lambda: "2026-01-01T00:00:00+00:00")
    monkeypatch.setattr(preflight_module._huggingface_hub, "HfApi", hfapi_factory)
    return log


def _publish_full_run(
    monkeypatch: pytest.MonkeyPatch,
    source_root: Path,
    data_root: Path,
) -> None:
    log = _install_subprocess_recorder(monkeypatch)
    exit_code = cli_run(
        [
            "run-and-publish",
            "--source-root",
            str(source_root),
            "--data-root",
            str(data_root),
            "--confirm-repo",
            REPO_ID,
        ]
    )
    assert exit_code == 0
    assert log["uploads"] >= 3
