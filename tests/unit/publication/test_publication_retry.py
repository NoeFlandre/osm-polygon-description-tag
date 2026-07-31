"""Tests for upload retry classification and bounded backoff in publication."""

from __future__ import annotations

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
    _classify_failure,
    _default_runner_with_retry,
    create_upload_plan,
    execute_upload,
)
from osm_polygon_description_tag.storage import write_geoparquet
from tests.conftest import make_record_dict


def _setup_dataset(data_root: Path) -> None:
    (data_root / "data").mkdir(parents=True)
    (data_root / "manifests").mkdir(parents=True)
    (data_root / "README.md").write_text("# Card\n", encoding="utf-8")
    (data_root / "stats.json").write_text("{}\n", encoding="utf-8")
    (data_root / "assets").mkdir()
    (data_root / "assets" / "description_polygon_density.png").write_bytes(
        b"\x89PNG\r\n\x1a\n" + b"map" * 1024
    )
    (data_root / "assets" / "area_distribution.png").write_bytes(
        b"\x89PNG\r\n\x1a\n" + b"hist" * 1024
    )
    source_root = data_root.parent / "raw"
    source_root.mkdir(exist_ok=True)
    source = source_root / "a.osm.pbf"
    source.write_bytes(b"a-bytes")
    record = make_record_dict(
        Polygon([(0, 0), (0, 1), (1, 1), (1, 0)]),
        {"description": "x"},
        osm_id=1,
        source_pbf="a.osm.pbf",
    )
    output = data_root / "data" / "a.parquet"
    write_geoparquet(iter([record]), output, batch_size=10)
    write_manifest(
        Manifest(
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
            code_revision="abc",
            started_at="2026-07-27T00:00:00+00:00",
            completed_at="2026-07-27T00:01:00+00:00",
            counts=RunCounts(emitted_features=1, included_rows=1, rejections={}),
        ),
        data_root / "manifests" / "a.manifest.json",
    )


def test_classify_failure_marks_retryable_exit_codes() -> None:
    for code in (5, 429, 502, 503, 504):
        completed = subprocess.CompletedProcess([], returncode=code)
        error = subprocess.CalledProcessError(code, [])
        error.completed = completed  # type: ignore[attr-defined]
        retryable, exit_code, kind = _classify_failure(error)
        assert retryable is True
        assert exit_code == code


def test_classify_failure_marks_non_retryable_exit_codes() -> None:
    for code in (1, 2, 3, 4):
        completed = subprocess.CompletedProcess([], returncode=code)
        error = subprocess.CalledProcessError(code, [])
        error.completed = completed  # type: ignore[attr-defined]
        retryable, _, _ = _classify_failure(error)
        assert retryable is False


def test_classify_failure_detects_timeout_in_stderr() -> None:
    completed = subprocess.CompletedProcess([], returncode=1, stderr=b"connection timeout")
    error = subprocess.CalledProcessError(1, [])
    error.completed = completed  # type: ignore[attr-defined]
    retryable, _, kind = _classify_failure(error)
    assert retryable is True
    assert kind == "timeout"


def test_default_runner_with_retry_returns_on_success() -> None:
    # /bin/echo always succeeds; this exercises the happy path.
    _default_runner_with_retry(["echo", "ok"], max_retries=1, backoff_seconds=0.0)


def test_default_runner_with_retry_raises_after_max_attempts() -> None:
    with pytest.raises(subprocess.CalledProcessError):
        _default_runner_with_retry(["/bin/sh", "-c", "exit 1"], max_retries=2, backoff_seconds=0.0)


def test_execute_upload_rejects_missing_confirmation(tmp_path: Path) -> None:
    data_root = tmp_path / "generated"
    _setup_dataset(data_root)
    plan = create_upload_plan(data_root)

    with pytest.raises(PublicationError, match="confirmation required"):
        execute_upload(plan, confirmation=None, runner=lambda _: None)


def test_execute_upload_rejects_empty_manifest(tmp_path: Path) -> None:
    data_root = tmp_path / "generated"
    _setup_dataset(data_root)
    # Replace the manifest with a placeholder "{}" to simulate stale state.
    (data_root / "manifests" / "a.manifest.json").write_text("{}\n", encoding="utf-8")

    with pytest.raises(PublicationError, match="manifest"):
        create_upload_plan(data_root)


def test_execute_upload_rejects_invalid_manifest_json(tmp_path: Path) -> None:
    data_root = tmp_path / "generated"
    _setup_dataset(data_root)
    (data_root / "manifests" / "a.manifest.json").write_text("{not valid}", encoding="utf-8")

    with pytest.raises(PublicationError, match="invalid manifest"):
        create_upload_plan(data_root)


def test_execute_upload_rejects_mismatched_parquet(tmp_path: Path) -> None:
    data_root = tmp_path / "generated"
    _setup_dataset(data_root)
    # Mutate the parquet after writing the manifest so the output identity drifts.
    (data_root / "data" / "a.parquet").write_bytes(b"different")

    with pytest.raises(PublicationError, match="identity"):
        create_upload_plan(data_root)
