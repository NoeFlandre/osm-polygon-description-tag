"""Shared local Grid workflow fixtures.

These fixtures centralize the synthetic source, snapshot, project, and job
layouts used by the Grid contract tests. Keeping the shape in one place means
schema or bundle changes fail every dependent test together.
"""

from pathlib import Path

import pytest
from shapely.geometry import Polygon

from osm_polygon_description_tag.dataset.languages.snapshot import (
    SnapshotManifest,
    fingerprint_lockfile,
    fingerprint_project_source,
    prepare_snapshot,
)
from osm_polygon_description_tag.dataset.storage import write_geoparquet
from osm_polygon_description_tag.workflow.grid_operator import prepare_job
from tests.conftest import make_record_dict

SHARD = "region.parquet"
REMOTE = {
    "remote_project_dir": "/home/user/project",
    "remote_source_dir": "/scratch/staging/source",
    "remote_run_dir": "/scratch/staging/run",
    "sat_model_path": "/home/user/models/sat-3l-sm/model.safetensors",
}
REMOTE_BUNDLE = "/scratch/lang-bundle"


def _write_rows(source: Path, *, count: int, description: str, batch_size: int = 2) -> None:
    source.mkdir(parents=True, exist_ok=True)
    write_geoparquet(
        (
            make_record_dict(
                Polygon([(0, 0), (0, 1), (1, 1), (1, 0)]),
                {"description": f"{description} {index}"},
                osm_id=index + 1,
            )
            for index in range(count)
        ),
        source / SHARD,
        batch_size=batch_size,
    )


def _write_project(project: Path) -> None:
    (project / "src").mkdir(parents=True)
    (project / "src" / "module.py").write_text("VALUE = 1\n", encoding="utf-8")
    (project / "pyproject.toml").write_text("[project]\nname = 'synthetic'\n", encoding="utf-8")
    (project / "uv.lock").write_text("version = 1\n", encoding="utf-8")


def _prepare_snapshot(source: Path, run: Path) -> SnapshotManifest:
    return prepare_snapshot(source, run, code_fingerprint="a" * 64, lock_fingerprint="b" * 64)


@pytest.fixture
def prepared(tmp_path: Path) -> tuple[Path, Path, SnapshotManifest]:
    source = tmp_path / "source"
    _write_rows(source, count=4, description="A synthetic description")
    run = tmp_path / "run"
    return source, run, _prepare_snapshot(source, run)


@pytest.fixture
def empty_prepared(tmp_path: Path) -> tuple[Path, Path, SnapshotManifest]:
    source = tmp_path / "source"
    _write_rows(source, count=0, description="An empty description")
    run = tmp_path / "run"
    return source, run, _prepare_snapshot(source, run)


@pytest.fixture
def portable_prepared(tmp_path: Path) -> tuple[Path, Path, Path, SnapshotManifest]:
    source = tmp_path / "source"
    _write_rows(source, count=4, description="A portable description")
    project = tmp_path / "project"
    _write_project(project)
    run = tmp_path / "run"
    snapshot = prepare_snapshot(
        source,
        run,
        code_fingerprint=fingerprint_project_source(project),
        lock_fingerprint=fingerprint_lockfile(project),
    )
    return project, source, run, snapshot


@pytest.fixture
def portable(portable_prepared: tuple[Path, Path, Path, SnapshotManifest]):
    return portable_prepared


@pytest.fixture
def portable_inputs(tmp_path: Path) -> tuple[Path, Path, Path, SnapshotManifest]:
    source = tmp_path / "source"
    _write_rows(source, count=1, description="A portable description", batch_size=1)
    project = tmp_path / "project"
    _write_project(project)
    run = tmp_path / "run"
    snapshot = prepare_snapshot(
        source,
        run,
        code_fingerprint=fingerprint_project_source(project),
        lock_fingerprint=fingerprint_lockfile(project),
    )
    return project, source, run, snapshot


@pytest.fixture
def inputs(portable_inputs: tuple[Path, Path, Path, SnapshotManifest]):
    return portable_inputs


@pytest.fixture
def job(prepared: tuple[Path, Path, SnapshotManifest]):
    _, run, snapshot = prepared
    return run, *prepare_job(run, snapshot, SHARD, **REMOTE)


@pytest.fixture
def collection_runs(
    prepared: tuple[Path, Path, SnapshotManifest], tmp_path: Path
) -> tuple[Path, Path, Path, SnapshotManifest]:
    source, local, snapshot = prepared
    incoming = tmp_path / "retrieved"
    prepare_snapshot(
        source,
        incoming,
        code_fingerprint=snapshot.code_fingerprint,
        lock_fingerprint=snapshot.lock_fingerprint,
    )
    return source, local, incoming, snapshot


@pytest.fixture
def two_jobs(tmp_path: Path):
    """Prepare two ordered shard jobs for run-wide intent blocking tests."""
    source = tmp_path / "source"
    source.mkdir(parents=True)
    for index, name in enumerate((SHARD, "other.parquet")):
        write_geoparquet(
            (
                make_record_dict(
                    Polygon([(0, 0), (0, 1), (1, 1), (1, 0)]),
                    {"description": f"A synthetic description {index}-{row}"},
                    osm_id=index * 100 + row + 1,
                )
                for row in range(2)
            ),
            source / name,
            batch_size=2,
        )
    run = tmp_path / "run"
    snapshot = _prepare_snapshot(source, run)
    jobs = [prepare_job(run, snapshot, name, **REMOTE) for name in (SHARD, "other.parquet")]
    jobs.sort(key=lambda item: item[1].root.name)
    return run, jobs[0], jobs[1]
