"""Public CLI stream and exit-code contracts independent of parser ownership."""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from shapely import to_wkb
from shapely.geometry import Polygon

import osm_polygon_description_tag.cli as cli
import osm_polygon_description_tag.workflow.orchestrator as workflow_orchestrator
from osm_polygon_description_tag.osm.discovery import Source
from osm_polygon_description_tag.osm.extraction import ExportRecord
from osm_polygon_description_tag.publication.models import UploadItem, UploadPlan
from osm_polygon_description_tag.workflow.build import BuildResult
from osm_polygon_description_tag.workflow.orchestrator import (
    OrchestrationReport,
    SourceOutcome,
)


def _inspect_args(source_root: Path, data_root: Path) -> list[str]:
    return [
        "inspect",
        "--source-root",
        str(source_root),
        "--data-root",
        str(data_root),
    ]


def _common_args(source_root: Path, data_root: Path) -> list[str]:
    return [
        "--source-root",
        str(source_root),
        "--data-root",
        str(data_root),
        "--osmium",
        "fake-osmium",
    ]


def _run_json(
    argv: list[str],
    capsys: pytest.CaptureFixture[str],
) -> tuple[int, dict[str, object]]:
    exit_code = cli.run(argv)
    captured = capsys.readouterr()
    decoder = json.JSONDecoder()
    payload, end = decoder.raw_decode(captured.out)
    assert captured.out[end:].strip() == ""
    assert captured.err == ""
    assert "\x1b[" not in captured.out
    assert "\r" not in captured.out
    return exit_code, payload


@pytest.fixture
def cli_roots(tmp_path: Path) -> tuple[Path, Path]:
    source_root = tmp_path / "raw"
    source_root.mkdir()
    data_root = tmp_path / "generated"
    return source_root, data_root


def test_inspect_success_payload_is_exact(
    monkeypatch: pytest.MonkeyPatch,
    cli_roots: tuple[Path, Path],
    capsys: pytest.CaptureFixture[str],
) -> None:
    source_root, data_root = cli_roots
    source_path = source_root / "region.osm.pbf"
    source = Source(source_path, "region.osm.pbf", "region.parquet", 9, 123)
    export_config = source_root.parent / "osmium-export.json"
    monkeypatch.setattr(cli, "discover_sources", lambda _root: (source,))
    monkeypatch.setattr(cli, "osmium_export_config", lambda: export_config)

    exit_code, payload = _run_json(["inspect", *_common_args(source_root, data_root)], capsys)

    assert exit_code == 0
    assert payload == {
        "source_root": str(source_root),
        "data_root": str(data_root),
        "osmium_executable": "fake-osmium",
        "export_config": str(export_config),
        "source_count": 1,
        "sources": [
            {
                "name": "region.osm.pbf",
                "output_name": "region.parquet",
                "size_bytes": 9,
                "mtime_ns": 123,
            }
        ],
    }


def test_build_one_success_payload_is_exact(
    monkeypatch: pytest.MonkeyPatch,
    cli_roots: tuple[Path, Path],
    capsys: pytest.CaptureFixture[str],
) -> None:
    source_root, data_root = cli_roots
    source = Source(source_root / "region.osm.pbf", "region.osm.pbf", "region.parquet", 9, 123)
    result = BuildResult(
        source_name=source.name,
        output_name=source.output_name,
        status="built",
        emitted_features=7,
        included_rows=5,
        rejections={"missing_description": 2},
        output_path=data_root / "data" / source.output_name,
        manifest_path=data_root / "manifests" / "region.manifest.json",
    )
    monkeypatch.setattr(cli, "discover_sources", lambda _root: (source,))
    monkeypatch.setattr(cli, "build_one", lambda *_args, **_kwargs: result)

    exit_code, payload = _run_json(
        ["build-one", *_common_args(source_root, data_root), source.name], capsys
    )

    assert exit_code == 0
    assert payload == {
        "source_name": "region.osm.pbf",
        "output_name": "region.parquet",
        "status": "built",
        "emitted_features": 7,
        "included_rows": 5,
        "rejections": {"missing_description": 2},
        "output_path": str(data_root / "data" / "region.parquet"),
        "manifest_path": str(data_root / "manifests" / "region.manifest.json"),
    }


def test_build_all_success_payload_is_exact(
    monkeypatch: pytest.MonkeyPatch,
    cli_roots: tuple[Path, Path],
    capsys: pytest.CaptureFixture[str],
) -> None:
    source_root, data_root = cli_roots
    source = Source(source_root / "region.osm.pbf", "region.osm.pbf", "region.parquet", 9, 123)
    result = BuildResult(
        source.name,
        source.output_name,
        "reused",
        7,
        5,
        {},
        data_root / "data" / source.output_name,
        data_root / "manifests" / "region.manifest.json",
    )
    monkeypatch.setattr(cli, "discover_sources", lambda _root: (source,))
    monkeypatch.setattr(cli, "build_all", lambda _sources, *, build: [result])

    exit_code, payload = _run_json(["build-all", *_common_args(source_root, data_root)], capsys)

    assert exit_code == 0
    assert payload == {
        "count": 1,
        "results": [
            {
                "source_name": "region.osm.pbf",
                "status": "reused",
                "included_rows": 5,
            }
        ],
    }


def test_validate_success_payload_is_exact(
    monkeypatch: pytest.MonkeyPatch,
    cli_roots: tuple[Path, Path],
    capsys: pytest.CaptureFixture[str],
) -> None:
    source_root, data_root = cli_roots
    data_dir = data_root / "data"
    data_dir.mkdir(parents=True)
    first = data_dir / "a.parquet"
    second = data_dir / "b.parquet"
    first.touch()
    second.touch()
    rows = {first: 2, second: 3}
    monkeypatch.setattr(cli, "validate_geoparquet", rows.__getitem__)

    exit_code, payload = _run_json(["validate", *_common_args(source_root, data_root)], capsys)

    assert exit_code == 0
    assert payload == {"files": 2, "rows": 5}


def test_generate_card_success_payload_is_exact(
    monkeypatch: pytest.MonkeyPatch,
    cli_roots: tuple[Path, Path],
    capsys: pytest.CaptureFixture[str],
) -> None:
    source_root, data_root = cli_roots
    monkeypatch.setattr(
        cli,
        "generate_dataset_docs",
        lambda _root, _template: {
            "output_files": 2,
            "rows": 8,
            "name_suffixes": {"description:en": 3},
            "ignored_internal_detail": True,
        },
    )

    exit_code, payload = _run_json(["generate-card", *_common_args(source_root, data_root)], capsys)

    assert exit_code == 0
    assert payload == {
        "output_files": 2,
        "rows": 8,
        "name_suffixes": {"description:en": 3},
    }


def _upload_plan(data_root: Path) -> UploadPlan:
    return UploadPlan(
        repo_id="NoeFlandre/osm-polygon-description-tag",
        data_root=str(data_root),
        files=(UploadItem("README.md", 12, "a" * 64),),
        identity_sha256="b" * 64,
    )


def test_publish_plan_success_payload_is_exact(
    monkeypatch: pytest.MonkeyPatch,
    cli_roots: tuple[Path, Path],
    capsys: pytest.CaptureFixture[str],
) -> None:
    source_root, data_root = cli_roots
    monkeypatch.setattr(cli, "create_upload_plan", lambda _root: _upload_plan(data_root))

    exit_code, payload = _run_json(["publish-plan", *_common_args(source_root, data_root)], capsys)

    assert exit_code == 0
    assert payload == {
        "repo_id": "NoeFlandre/osm-polygon-description-tag",
        "identity_sha256": "b" * 64,
        "files": [{"relative_path": "README.md", "sha256": "a" * 64}],
    }


def test_publish_success_payload_is_exact(
    monkeypatch: pytest.MonkeyPatch,
    cli_roots: tuple[Path, Path],
    capsys: pytest.CaptureFixture[str],
) -> None:
    source_root, data_root = cli_roots
    plan = _upload_plan(data_root)
    executions: list[tuple[UploadPlan, str]] = []
    monkeypatch.setattr(cli, "create_upload_plan", lambda _root: plan)
    monkeypatch.setattr(
        cli,
        "execute_upload",
        lambda actual, *, confirmation: executions.append((actual, confirmation)),
    )

    exit_code, payload = _run_json(
        ["publish", *_common_args(source_root, data_root), "--plan", "b" * 64],
        capsys,
    )

    assert exit_code == 0
    assert executions == [(plan, "b" * 64)]
    assert payload == {
        "repo_id": "NoeFlandre/osm-polygon-description-tag",
        "identity_sha256": "b" * 64,
    }


def _report() -> OrchestrationReport:
    return OrchestrationReport(
        source_count=1,
        preflight={"source_count": 1, "osmium_version": "fake 1.0"},
        outcomes=[
            SourceOutcome(
                source_name="region.osm.pbf",
                status="already-published",
                included_rows=5,
                output_bytes=42,
                remote_revision="revision-1",
                note=None,
            )
        ],
        final_remote_revision="revision-1",
    )


def test_run_and_publish_success_payload_is_exact(
    monkeypatch: pytest.MonkeyPatch,
    cli_roots: tuple[Path, Path],
    capsys: pytest.CaptureFixture[str],
) -> None:
    source_root, data_root = cli_roots
    monkeypatch.setattr(cli, "run_and_publish", lambda **_kwargs: _report())

    exit_code, payload = _run_json(
        [
            "run-and-publish",
            *_common_args(source_root, data_root),
            "--confirm-repo",
            "NoeFlandre/osm-polygon-description-tag",
        ],
        capsys,
    )

    assert exit_code == 0
    assert payload == {
        "preflight": {"source_count": 1, "osmium_version": "fake 1.0"},
        "source_count": 1,
        "outcomes": [
            {
                "source_name": "region.osm.pbf",
                "status": "already-published",
                "included_rows": 5,
                "output_bytes": 42,
                "remote_revision": "revision-1",
                "note": None,
            }
        ],
        "final_remote_revision": "revision-1",
    }


def test_domain_error_is_exact_plain_stderr(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    def fail_discovery(_root: Path) -> object:
        raise ValueError("boom")

    monkeypatch.setattr(cli, "discover_sources", fail_discovery)

    exit_code = cli.run(_inspect_args(tmp_path / "raw", tmp_path / "generated"))
    captured = capsys.readouterr()

    assert exit_code == 1
    assert captured.out == ""
    assert captured.err == "error: boom\n"
    assert "\x1b[" not in captured.err


def test_keyboard_interrupt_returns_130_without_output(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    def interrupt_discovery(_root: Path) -> object:
        raise KeyboardInterrupt

    monkeypatch.setattr(cli, "discover_sources", interrupt_discovery)

    exit_code = cli.run(_inspect_args(tmp_path / "raw", tmp_path / "generated"))
    captured = capsys.readouterr()

    assert exit_code == 130
    assert captured.out == ""
    assert captured.err == ""


def test_noninteractive_run_and_publish_keeps_progress_plain(
    monkeypatch: pytest.MonkeyPatch,
    cli_roots: tuple[Path, Path],
    capsys: pytest.CaptureFixture[str],
) -> None:
    source_root, data_root = cli_roots
    source_path = source_root / "region.osm.pbf"
    source_path.write_bytes(b"synthetic")

    geometry = Polygon([(0, 0), (0, 1), (1, 1), (1, 0)])
    record = ExportRecord(
        geometry_ewkb_hex=to_wkb(
            geometry, include_srid=True, flavor="extended", byte_order=1
        ).hex(),
        osm_type="way",
        osm_id=1,
        version=1,
        changeset=1,
        timestamp="2026-01-01T00:00:00Z",
        tags={"description": "synthetic"},
    )

    def run_real_orchestrator(**kwargs: object) -> OrchestrationReport:
        return workflow_orchestrator.run_and_publish(
            **kwargs,
            preflight=lambda: {
                "osmium_executable": "fake-osmium",
                "osmium_version": "osmium version 1.19.1",
                "hub_repo_sha": "preflight-sha",
                "source_count": 1,
            },
            exporter=lambda *_args, **_kwargs: iter((record,)),
            upload_runner=lambda _command: "upload-revision",
            verifier=lambda _repo_id, _files: "verified-revision",
            clock=lambda: "2026-07-30T12:00:00+00:00",
            progress_interval=1,
        )

    monkeypatch.setattr(cli, "run_and_publish", run_real_orchestrator)

    exit_code = cli.run(
        [
            "run-and-publish",
            *_common_args(source_root, data_root),
            "--confirm-repo",
            "NoeFlandre/osm-polygon-description-tag",
        ]
    )
    captured = capsys.readouterr()
    decoder = json.JSONDecoder()
    payload, end = decoder.raw_decode(captured.out)

    assert exit_code == 0
    assert payload["source_count"] == 1
    assert payload["outcomes"][0]["source_name"] == "region.osm.pbf"
    assert payload["outcomes"][0]["status"] == "built-needs-upload"
    assert captured.out[end:].strip() == ""
    assert " build_progress " in captured.err
    assert "source=region.osm.pbf" in captured.err
    assert "emitted=1 included=1" in captured.err
    assert "\x1b[" not in captured.err
    assert "\r" not in captured.err
    assert "it/s" not in captured.err
    assert "%|" not in captured.err
    assert "\x1b[" not in captured.out
    assert "\r" not in captured.out
