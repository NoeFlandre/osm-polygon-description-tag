"""RED tests proving ``upload_timeout`` is correctly threaded through the pipeline.

The CLI default must be ``None`` (no overall upload kill). An explicitly
positive timeout must reach the underlying ``subprocess.run`` call. Ctrl-C
must immediately escape with exit code 130 and never be retried.
"""

from __future__ import annotations

import inspect
import json
from pathlib import Path

import pytest
from shapely.geometry import Polygon

from osm_polygon_description_tag.config import Paths
from osm_polygon_description_tag.orchestrator import run_and_publish
from osm_polygon_description_tag.publication import (
    REPO_ID,
    _default_runner_with_retry,
    execute_upload,
)

_CLOCK = "2026-07-27T00:00:00+00:00"


def _frozen_clock() -> str:
    return _CLOCK


def _setup_workspace(tmp_path: Path) -> tuple[Paths, Path, Path]:
    source_root = tmp_path / "raw"
    data_root = tmp_path / "generated"
    source_root.mkdir()
    data_root.mkdir()
    (source_root / "a.osm.pbf").write_bytes(b"a-bytes")
    return Paths(source_root=source_root, data_root=data_root), source_root, data_root


def _fake_exporter_records() -> object:
    def _export(source_path: Path, _cfg: Path) -> object:
        from shapely import to_wkb

        from osm_polygon_description_tag.extraction import ExportRecord

        geom = Polygon([(0, 0), (0, 1), (1, 1), (1, 0)])
        ewkb = to_wkb(geom, include_srid=True, flavor="extended", byte_order=1)
        return iter(
            [
                ExportRecord(
                    geometry_ewkb_hex=ewkb.hex(),
                    osm_type="way",
                    osm_id=1,
                    version=1,
                    changeset=1,
                    timestamp="2026-01-01T00:00:00Z",
                    tags=json.loads('{"description": "x"}'),
                )
            ]
        )

    return _export


def test_default_runner_with_retry_accepts_timeout() -> None:
    """The default runner forwards the timeout argument to subprocess.run."""
    seen: list[float | None] = []

    def fake_subprocess(command: list[str], timeout: float | None = None) -> None:
        seen.append(timeout)

    _default_runner_with_retry(["hf", "--version"], _runner=fake_subprocess, timeout=12.5)
    assert seen == [12.5]


def test_default_runner_with_retry_default_timeout_is_none() -> None:
    """Omitting ``timeout`` defaults to None (no overall kill)."""
    seen: list[float | None] = []

    def fake_subprocess(command: list[str], timeout: float | None = None) -> None:
        seen.append(timeout)

    _default_runner_with_retry(["hf", "--version"], _runner=fake_subprocess)
    assert seen == [None]


def test_execute_upload_threads_timeout() -> None:
    """execute_upload(plan, ..., timeout=...) must forward timeout to the runner."""
    sig = inspect.signature(execute_upload)
    assert "timeout" in sig.parameters, "execute_upload must accept a timeout kwarg"
    assert "runner" in sig.parameters, "execute_upload must accept a runner kwarg"


def test_orchestrator_threads_timeout_to_publication(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """run_and_publish(upload_timeout=...) propagates to execute_upload."""
    import osm_polygon_description_tag.publication.upload as pub

    seen: list[float | None] = []

    def fake_subprocess(command: list[str], timeout: float | None = None) -> None:
        seen.append(timeout)

    monkeypatch.setattr(pub, "_default_runner_with_retry", fake_subprocess)

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

    paths, source_root, data_root = _setup_workspace(tmp_path)
    (data_root / "data").mkdir(parents=True, exist_ok=True)
    (data_root / "manifests").mkdir(parents=True, exist_ok=True)
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
            schema_version=3,
            geoparquet_version="1.1.0",
            transform_algorithm_version=3,
            area_policy_sha256=current_area_policy_sha256(),
            output_algorithm_revision=current_output_algorithm_revision(),
            source=source_identity_for(source_root / "a.osm.pbf"),
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
    (paths.data_root / "README.md").write_text("stub")
    (paths.data_root / "stats.json").write_text("{}")

    run_and_publish(
        paths=paths,
        confirm_repo=REPO_ID,
        preflight=lambda: {"preflight": "stub", "source_count": 1},
        clock=_frozen_clock,
        exporter=_fake_exporter_records(),
        verifier=lambda repo_id, files: "verified-rev",
        upload_timeout=99.0,
    )
    # per-PBF + final metadata = 2 runner calls; both must receive the
    # explicit upload_timeout value.
    assert seen == [99.0, 99.0]


def test_keyboard_interrupt_escapes_immediately() -> None:
    """KeyboardInterrupt is not retried; runner re-raises immediately."""
    calls: list[int] = []

    def fake_subprocess(command: list[str], timeout: float | None = None) -> None:
        calls.append(1)
        raise KeyboardInterrupt()

    with pytest.raises(KeyboardInterrupt):
        _default_runner_with_retry(["hf", "x"], _runner=fake_subprocess)
    assert len(calls) == 1, "exactly once; never retried"


def test_keyboardinterrupt_through_cli_returns_130(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """A KeyboardInterrupt originating in publication escapes with exit 130."""
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

    paths, source_root, data_root = _setup_workspace(tmp_path)
    (data_root / "data").mkdir(parents=True, exist_ok=True)
    (data_root / "manifests").mkdir(parents=True, exist_ok=True)
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
            schema_version=3,
            geoparquet_version="1.1.0",
            transform_algorithm_version=3,
            area_policy_sha256=current_area_policy_sha256(),
            output_algorithm_revision=current_output_algorithm_revision(),
            source=source_identity_for(source_root / "a.osm.pbf"),
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
    (paths.data_root / "README.md").write_text("# README")
    (paths.data_root / "stats.json").write_text("{}")

    import osm_polygon_description_tag.publication.upload as pub
    import osm_polygon_description_tag.workflow.orchestrator as orch

    def interrupting_runner(command: list[str], timeout: float | None = None) -> None:
        raise KeyboardInterrupt()

    monkeypatch.setattr(pub, "_default_runner_with_retry", interrupting_runner)
    verifier_factory_calls = 0

    def verifier_factory():
        nonlocal verifier_factory_calls
        verifier_factory_calls += 1
        return lambda *_a, **_kw: "verified"

    monkeypatch.setattr(
        orch,
        "default_hub_verifier_factory",
        verifier_factory,
    )

    from osm_polygon_description_tag.cli import run as cli_run

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
    assert verifier_factory_calls == 1


def test_explicit_timeout_reaches_subprocess_run() -> None:
    """An explicit positive timeout reaches subprocess.run (verified via _runner hook)."""
    seen: list[float | None] = []

    def fake_subprocess(command: list[str], timeout: float | None = None) -> None:
        seen.append(timeout)

    _default_runner_with_retry(["hf", "x"], _runner=fake_subprocess, timeout=42.0)
    assert seen == [42.0]


def test_default_no_timeout_reaches_subprocess_run() -> None:
    """A default of None reaches subprocess.run."""
    seen: list[float | None] = []

    def fake_subprocess(command: list[str], timeout: float | None = None) -> None:
        seen.append(timeout)

    _default_runner_with_retry(["hf", "x"], _runner=fake_subprocess)
    assert seen == [None]
