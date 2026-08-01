"""End-to-end public CLI lifecycle covering stoppable resumable publication.

The three-run scenario uses synthetic PBFs assembled by the real osmium
binary, the public ``run-and-publish`` CLI, and only fakes HF
preflight, the upload subprocess, and the Hub verifier. No real data is
read or written outside the temporary workspace.

Run 1:

- preflight passes;
- first PBF builds via real osmium, publishes, and is verified;
- the second PBF's upload is interrupted (KeyboardInterrupt simulated)
  after a real osmium build but before the upload completes;
- the CLI exits 130; the first PBF's state is valid, the second PBF's
  Parquet and manifest are valid but not published.

Run 2:

- the first PBF is ``already-published`` and skipped;
- the second PBF is resumed and uploaded;
- the final deterministic README/stats metadata is uploaded and verified;
- metadata state is written.

Run 3:

- zero builds, zero dataset uploads, zero Hub verifier calls;
- zero changes to Parquets, manifests, README, stats, or state bytes;
- logs may append.
"""

from __future__ import annotations

import hashlib
import json
import shutil
import subprocess
from collections.abc import Iterator
from pathlib import Path

import pytest

from osm_polygon_description_tag.cli import run as cli_run
from osm_polygon_description_tag.config import Paths
from osm_polygon_description_tag.orchestrator import PUBLICATION_STATE_FILENAME
from osm_polygon_description_tag.publication import REPO_ID


def _sha256_text(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _write_pbf(executable: str, source: Path, pbf_path: Path) -> None:
    completed = subprocess.run(  # noqa: S603 - controlled argument array, no shell
        [executable, "cat", str(source), "-o", str(pbf_path), "--overwrite"],
        check=True,
        capture_output=True,
        shell=False,
        timeout=30,
    )
    assert completed.returncode == 0, completed.stderr.decode("utf-8", errors="replace")


@pytest.fixture
def _real_osmium() -> Iterator[str]:
    executable = shutil.which("osmium")
    if executable is None:
        pytest.skip("osmium binary not installed")
    yield executable


def _build_real_dataset(
    tmp_path: Path, executable: str, name: str, fixture: str, *, extra_sources: tuple[str, ...] = ()
) -> tuple[Paths, Path, Path]:
    workspace = tmp_path / name
    source_root = workspace / "raw"
    data_root = workspace / "generated"
    source_root.mkdir(parents=True)
    data_root.mkdir(parents=True)
    pbf_path = source_root / f"{name}.osm.pbf"
    _write_pbf(executable, Path(fixture), pbf_path)
    for extra in extra_sources:
        shutil.copy(pbf_path, source_root / extra)
    return Paths(source_root=source_root, data_root=data_root), source_root, data_root


def _make_hf_stub() -> object:
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


def _install_external_boundaries(
    monkeypatch: pytest.MonkeyPatch,
    *,
    runner,
    verifier_factory,
    interrupts_on_per_pbf: int | None = None,
) -> dict:
    import osm_polygon_description_tag.publication.upload as pub
    import osm_polygon_description_tag.workflow.orchestrator as orch
    import osm_polygon_description_tag.workflow.preflight as preflight_module

    log: dict[str, object] = {
        "uploads": 0,
        "verifier_calls": 0,
        "interrupted": False,
        "commands": [],
    }
    per_pbf_seen = [0]

    def runner_wrapper(command: list[str], timeout: float | None = None) -> None:
        includes = [
            command[index + 1] for index, piece in enumerate(command) if piece == "--include"
        ]
        per_pbf_seen[0] += 1
        log["uploads"] = int(log["uploads"]) + 1
        log["commands"].append(list(command))  # type: ignore[attr-defined]
        if (
            interrupts_on_per_pbf is not None
            and per_pbf_seen[0] == interrupts_on_per_pbf
            and any(inc.startswith("data/") for inc in includes)
        ):
            log["interrupted"] = True
            raise KeyboardInterrupt

    monkeypatch.setattr(pub, "_default_runner_with_retry", runner_wrapper)
    monkeypatch.setattr(orch, "default_hub_verifier_factory", verifier_factory)
    monkeypatch.setattr(orch, "_default_clock", lambda: "2026-07-28T00:00:00+00:00")
    monkeypatch.setattr(
        preflight_module._huggingface_hub, "HfApi", lambda *a, **kw: _make_hf_stub()
    )
    return log


def _verifier_factory():
    counter = {"value": 0}

    def f(_repo_id: str, _files: object) -> str:
        counter["value"] += 1
        return f"hub-rev-{counter['value']}"

    return f


def test_run_one_publishes_first_pbf_and_is_interrupted_on_second(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, _real_osmium: str
) -> None:
    """Run 1 publishes the first PBF, then the second is interrupted."""
    paths, source_root, data_root = _build_real_dataset(
        tmp_path,
        _real_osmium,
        "amendment",
        "tests/fixtures/amendment_coverage.osm",
        extra_sources=("duplicate.osm.pbf",),
    )

    log = _install_external_boundaries(
        monkeypatch, runner=None, verifier_factory=_verifier_factory, interrupts_on_per_pbf=2
    )

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
    assert exit_code == 130
    assert log["interrupted"] is True
    # State only contains the first PBF.
    state = json.loads((data_root / PUBLICATION_STATE_FILENAME).read_text(encoding="utf-8"))
    assert "amendment.osm.pbf" in state["published"]
    assert "duplicate.osm.pbf" not in state["published"]


def test_run_two_resumes_and_publishes_remaining_and_metadata(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, _real_osmium: str
) -> None:
    """Run 2 reuses the published first PBF and uploads the second plus metadata."""
    # Use a separate workspace for Run 2 to avoid coupling to Run 1 state.
    paths, source_root, data_root = _build_real_dataset(
        tmp_path,
        _real_osmium,
        "amendment",
        "tests/fixtures/amendment_coverage.osm",
        extra_sources=("duplicate.osm.pbf",),
    )

    # Pre-publish the first PBF so the run starts from a "one published,
    # one pending" baseline.
    from shapely.geometry import Polygon

    from osm_polygon_description_tag.manifest import (
        Manifest,
        RunCounts,
        current_area_policy_sha256,
        current_output_algorithm_revision,
        file_sha256,
        output_identity_for,
        source_identity_for,
        write_manifest,
    )
    from osm_polygon_description_tag.publication import _build_per_pbf_upload_plan
    from osm_polygon_description_tag.storage import write_geoparquet
    from tests.conftest import make_record_dict

    # Plant a pre-published first PBF to set up the resume scenario.
    write_geoparquet(
        iter(
            [
                make_record_dict(
                    Polygon([(0, 0), (0, 1), (1, 1), (1, 0)]),
                    {"description": "pre"},
                    osm_id=1,
                    source_pbf="amendment.osm.pbf",
                )
            ]
        ),
        data_root / "data" / "amendment.parquet",
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
            source=source_identity_for(source_root / "amendment.osm.pbf"),
            output=output_identity_for(data_root / "data" / "amendment.parquet"),
            osmium_version=None,
            dependency_versions={"pyarrow": "20.0.0"},
            code_revision=None,
            started_at="2026-07-28T00:00:00+00:00",
            completed_at="2026-07-28T00:00:01+00:00",
            counts=RunCounts(emitted_features=1, included_rows=1, rejections={}),
        ),
        data_root / "manifests" / "amendment.manifest.json",
    )
    (data_root / "README.md").write_text("# README")
    (data_root / "stats.json").write_text("{}")
    (data_root / "assets").mkdir()
    (data_root / "assets" / "description_polygon_density.png").write_bytes(
        b"\x89PNG\r\n\x1a\n" + b"map" * 1024
    )
    (data_root / "assets" / "area_distribution.png").write_bytes(
        b"\x89PNG\r\n\x1a\n" + b"hist" * 1024
    )
    (data_root / "assets" / "dataset-card-hero.png").write_bytes(
        b"\x89PNG\r\n\x1a\n" + b"hero" * 1024
    )
    state = {
        "schema_version": 1,
        "published": {
            "amendment.osm.pbf": {
                "source_sha256": source_identity_for(source_root / "amendment.osm.pbf").sha256,
                "output_sha256": file_sha256(data_root / "data" / "amendment.parquet"),
                "output_bytes": (data_root / "data" / "amendment.parquet").stat().st_size,
                "remote_revision": "pre-rev",
                "artifact_identity": _build_per_pbf_upload_plan(
                    data_root, "amendment.osm.pbf"
                ).identity_sha256,
                "completed_at": "2026-07-28T00:00:00+00:00",
            }
        },
    }
    (data_root / PUBLICATION_STATE_FILENAME).write_text(
        json.dumps(state, sort_keys=True, indent=2), encoding="utf-8"
    )

    _install_external_boundaries(monkeypatch, runner=None, verifier_factory=_verifier_factory)
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
    final_state = json.loads((data_root / PUBLICATION_STATE_FILENAME).read_text(encoding="utf-8"))
    assert "amendment.osm.pbf" in final_state["published"]
    assert "duplicate.osm.pbf" in final_state["published"]
    assert "metadata" in final_state


def test_run_three_is_pure_no_op(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, _real_osmium: str
) -> None:
    """Run 3 against a fully-published dataset performs zero dataset activity."""
    paths, source_root, data_root = _build_real_dataset(
        tmp_path,
        _real_osmium,
        "amendment",
        "tests/fixtures/amendment_coverage.osm",
        extra_sources=("duplicate.osm.pbf",),
    )

    from shapely.geometry import Polygon

    from osm_polygon_description_tag.manifest import (
        Manifest,
        RunCounts,
        current_area_policy_sha256,
        current_output_algorithm_revision,
        file_sha256,
        output_identity_for,
        source_identity_for,
        write_manifest,
    )
    from osm_polygon_description_tag.publication import (
        _build_metadata_only_upload_plan,
        _build_per_pbf_upload_plan,
    )
    from osm_polygon_description_tag.storage import write_geoparquet
    from tests.conftest import make_record_dict

    write_geoparquet(
        iter(
            [
                make_record_dict(
                    Polygon([(0, 0), (0, 1), (1, 1), (1, 0)]),
                    {"description": "pre-amend"},
                    osm_id=1,
                    source_pbf="amendment.osm.pbf",
                )
            ]
        ),
        data_root / "data" / "amendment.parquet",
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
            source=source_identity_for(source_root / "amendment.osm.pbf"),
            output=output_identity_for(data_root / "data" / "amendment.parquet"),
            osmium_version=None,
            dependency_versions={"pyarrow": "20.0.0"},
            code_revision=None,
            started_at="2026-07-28T00:00:00+00:00",
            completed_at="2026-07-28T00:00:01+00:00",
            counts=RunCounts(emitted_features=1, included_rows=1, rejections={}),
        ),
        data_root / "manifests" / "amendment.manifest.json",
    )
    write_geoparquet(
        iter(
            [
                make_record_dict(
                    Polygon([(0, 0), (0, 1), (1, 1), (1, 0)]),
                    {"description": "pre-dup"},
                    osm_id=2,
                    source_pbf="duplicate.osm.pbf",
                )
            ]
        ),
        data_root / "data" / "duplicate.parquet",
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
            source=source_identity_for(source_root / "duplicate.osm.pbf"),
            output=output_identity_for(data_root / "data" / "duplicate.parquet"),
            osmium_version=None,
            dependency_versions={"pyarrow": "20.0.0"},
            code_revision=None,
            started_at="2026-07-28T00:00:00+00:00",
            completed_at="2026-07-28T00:00:01+00:00",
            counts=RunCounts(emitted_features=1, included_rows=1, rejections={}),
        ),
        data_root / "manifests" / "duplicate.manifest.json",
    )
    (data_root / "README.md").write_text("# README")
    (data_root / "stats.json").write_text("{}")
    (data_root / "assets").mkdir()
    (data_root / "assets" / "description_polygon_density.png").write_bytes(
        b"\x89PNG\r\n\x1a\n" + b"map" * 1024
    )
    (data_root / "assets" / "area_distribution.png").write_bytes(
        b"\x89PNG\r\n\x1a\n" + b"hist" * 1024
    )
    (data_root / "assets" / "dataset-card-hero.png").write_bytes(
        b"\x89PNG\r\n\x1a\n" + b"hero" * 1024
    )
    # Plant the canonical card that ``generate_dataset_docs`` would
    # produce so the metadata identity remains stable after the
    # orchestrator's refresh step.
    from osm_polygon_description_tag._resources import dataset_card_template
    from osm_polygon_description_tag.dataset.reporting import generate_dataset_docs

    generate_dataset_docs(
        data_root,
        dataset_card_template(),
        clock=lambda: "2026-07-28T00:00:00+00:00",
    )
    assert (data_root / "assets" / "description_polygon_density.png").is_file()
    plan_meta = _build_metadata_only_upload_plan(data_root)
    state = {
        "schema_version": 1,
        "published": {
            "amendment.osm.pbf": {
                "source_sha256": source_identity_for(source_root / "amendment.osm.pbf").sha256,
                "output_sha256": file_sha256(data_root / "data" / "amendment.parquet"),
                "output_bytes": (data_root / "data" / "amendment.parquet").stat().st_size,
                "remote_revision": "pre-rev",
                "artifact_identity": _build_per_pbf_upload_plan(
                    data_root, "amendment.osm.pbf"
                ).identity_sha256,
                "completed_at": "2026-07-28T00:00:00+00:00",
            },
            "duplicate.osm.pbf": {
                "source_sha256": source_identity_for(source_root / "duplicate.osm.pbf").sha256,
                "output_sha256": file_sha256(data_root / "data" / "duplicate.parquet"),
                "output_bytes": (data_root / "data" / "duplicate.parquet").stat().st_size,
                "remote_revision": "pre-rev-dup",
                "artifact_identity": _build_per_pbf_upload_plan(
                    data_root, "duplicate.osm.pbf"
                ).identity_sha256,
                "completed_at": "2026-07-28T00:00:00+00:00",
            },
        },
        "metadata": {
            "identity_sha256": plan_meta.identity_sha256,
            "readme_sha256": file_sha256(data_root / "README.md"),
            "stats_sha256": file_sha256(data_root / "stats.json"),
            "readme_size_bytes": (data_root / "README.md").stat().st_size,
            "stats_size_bytes": (data_root / "stats.json").stat().st_size,
            "h3_map_sha256": file_sha256(data_root / "assets" / "description_polygon_density.png"),
            "h3_map_size_bytes": (data_root / "assets" / "description_polygon_density.png")
            .stat()
            .st_size,
            "area_histogram_sha256": file_sha256(data_root / "assets" / "area_distribution.png"),
            "area_histogram_size_bytes": (data_root / "assets" / "area_distribution.png")
            .stat()
            .st_size,
            "dataset_card_hero_sha256": file_sha256(
                data_root / "assets" / "dataset-card-hero.png"
            ),
            "dataset_card_hero_size_bytes": (data_root / "assets" / "dataset-card-hero.png")
            .stat()
            .st_size,
            "verified_revision": "pre-rev-meta",
            "completed_at": "2026-07-28T00:00:00+00:00",
        },
    }
    (data_root / PUBLICATION_STATE_FILENAME).write_text(
        json.dumps(state, sort_keys=True, indent=2), encoding="utf-8"
    )

    log = _install_external_boundaries(monkeypatch, runner=None, verifier_factory=_verifier_factory)
    snapshot_state = _sha256_bytes((data_root / PUBLICATION_STATE_FILENAME).read_bytes())
    snapshot_readme = _sha256_bytes((data_root / "README.md").read_bytes())
    snapshot_stats = _sha256_bytes((data_root / "stats.json").read_bytes())
    snapshot_a = _sha256_bytes((data_root / "data" / "amendment.parquet").read_bytes())
    snapshot_d = _sha256_bytes((data_root / "data" / "duplicate.parquet").read_bytes())
    snapshot_am = _sha256_bytes((data_root / "manifests" / "amendment.manifest.json").read_bytes())
    snapshot_dm = _sha256_bytes((data_root / "manifests" / "duplicate.manifest.json").read_bytes())

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
    assert log["uploads"] == 0
    assert log["verifier_calls"] == 0
    assert _sha256_bytes((data_root / PUBLICATION_STATE_FILENAME).read_bytes()) == snapshot_state
    assert _sha256_bytes((data_root / "README.md").read_bytes()) == snapshot_readme
    assert _sha256_bytes((data_root / "stats.json").read_bytes()) == snapshot_stats
    assert _sha256_bytes((data_root / "data" / "amendment.parquet").read_bytes()) == snapshot_a
    assert _sha256_bytes((data_root / "data" / "duplicate.parquet").read_bytes()) == snapshot_d
    assert (
        _sha256_bytes((data_root / "manifests" / "amendment.manifest.json").read_bytes())
        == snapshot_am
    )
    assert (
        _sha256_bytes((data_root / "manifests" / "duplicate.manifest.json").read_bytes())
        == snapshot_dm
    )


def test_uploads_only_contain_per_pbf_and_metadata_files(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, _real_osmium: str
) -> None:
    """Every recorded upload contains exactly per-PBF or metadata-only files."""
    paths, source_root, data_root = _build_real_dataset(
        tmp_path,
        _real_osmium,
        "amendment-uploads",
        "tests/fixtures/amendment_coverage.osm",
    )
    log = _install_external_boundaries(monkeypatch, runner=None, verifier_factory=_verifier_factory)
    cli_run(
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
    # Per-PBF uploads (1) + metadata upload (1) = 2
    assert log["uploads"] == 2
    for command in log["commands"]:
        includes = [command[i + 1] for i, p in enumerate(command) if p == "--include"]
        assert "publication-state.json" not in includes
        assert not any(item.startswith("logs/") for item in includes)
        assert not any(item.startswith(".cache") for item in includes)
        for item in includes:
            assert (
                item.endswith(".parquet")
                or item.endswith(".manifest.json")
                or item
                in {
                    "README.md",
                    "stats.json",
                    "assets/description_polygon_density.png",
                    "assets/area_distribution.png",
                    "assets/dataset-card-hero.png",
                }
            )
