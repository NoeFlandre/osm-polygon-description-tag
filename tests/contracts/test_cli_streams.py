"""Public CLI stream and exit-code contracts independent of parser ownership."""

from __future__ import annotations

import hashlib
import json
import re
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from shapely import to_wkb
from shapely.geometry import Polygon

import osm_polygon_description_tag.publication.upload as publication_upload
import osm_polygon_description_tag.workflow.orchestrator as workflow_orchestrator
from osm_polygon_description_tag import cli, cli_handlers
from osm_polygon_description_tag.cli_requests import RunAndPublishRequest
from osm_polygon_description_tag.dataset import validation
from osm_polygon_description_tag.dataset.manifest import output_identity_for
from osm_polygon_description_tag.osm.discovery import Source
from osm_polygon_description_tag.osm.extraction import ExportRecord
from osm_polygon_description_tag.publication.models import UploadItem, UploadPlan
from osm_polygon_description_tag.runtime.presentation import TerminalPresenter
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
    monkeypatch.setattr(cli_handlers, "discover_sources", lambda _root: (source,))
    monkeypatch.setattr(cli_handlers, "osmium_export_config", lambda: export_config)

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
    monkeypatch.setattr(cli_handlers, "discover_sources", lambda _root: (source,))
    monkeypatch.setattr(cli_handlers, "build_one", lambda *_args, **_kwargs: result)

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
    monkeypatch.setattr(cli_handlers, "discover_sources", lambda _root: (source,))
    monkeypatch.setattr(cli_handlers, "build_all", lambda _sources, *, build: [result])

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
    _, data_root = cli_roots
    data_dir = data_root / "data"
    data_dir.mkdir(parents=True)
    first = data_dir / "a.parquet"
    second = data_dir / "b.parquet"
    first.touch()
    second.touch()
    rows = {first: 2, second: 3}
    manifests = (
        data_root / "manifests" / "a.manifest.json",
        data_root / "manifests" / "b.manifest.json",
    )
    monkeypatch.setattr(validation, "validate_geoparquet", lambda path, **_kwargs: rows[path])
    monkeypatch.setattr(
        validation,
        "validate_finalized_artifacts",
        lambda _root, **_kwargs: {
            "parquets": (first, second),
            "manifests": manifests,
            "manifest_records": tuple(
                SimpleNamespace(
                    source=SimpleNamespace(
                        name=f"{path.name.removesuffix('.manifest.json')}.osm.pbf"
                    ),
                    output=output_identity_for(parquet),
                    counts=SimpleNamespace(
                        included_rows=rows[parquet],
                        emitted_features=rows[parquet],
                        rejections={},
                    ),
                )
                for path, parquet in zip(manifests, (first, second), strict=True)
            ),
        },
    )

    exit_code, payload = _run_json(["validate", "--data-root", str(data_root)], capsys)

    assert exit_code == 0
    assert payload == {"files": 2, "rows": 5}


def test_generate_card_success_payload_is_exact(
    monkeypatch: pytest.MonkeyPatch,
    cli_roots: tuple[Path, Path],
    capsys: pytest.CaptureFixture[str],
) -> None:
    source_root, data_root = cli_roots
    monkeypatch.setattr(
        cli_handlers,
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
    monkeypatch.setattr(cli_handlers, "create_upload_plan", lambda _root: _upload_plan(data_root))

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
    monkeypatch.setattr(cli_handlers, "create_upload_plan", lambda _root: plan)
    monkeypatch.setattr(
        cli_handlers,
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


@pytest.mark.parametrize(
    ("failure", "message"),
    [
        (subprocess.CalledProcessError(7, ["hf", "upload"]), "upload failed with exit code 7"),
        (subprocess.TimeoutExpired(["hf", "upload"], 12.5), "upload timed out after 12.5 seconds"),
    ],
)
def test_publish_subprocess_failures_use_plain_cli_error_path(
    monkeypatch: pytest.MonkeyPatch,
    cli_roots: tuple[Path, Path],
    capsys: pytest.CaptureFixture[str],
    failure: BaseException,
    message: str,
) -> None:
    source_root, data_root = cli_roots
    content = b"metadata"
    data_root.mkdir(parents=True)
    (data_root / "README.md").write_bytes(content)
    plan = UploadPlan(
        repo_id="NoeFlandre/osm-polygon-description-tag",
        data_root=str(data_root),
        files=(
            UploadItem(
                "README.md",
                len(content),
                hashlib.sha256(content).hexdigest(),
            ),
        ),
        identity_sha256="identity",
    )
    monkeypatch.setattr(cli_handlers, "create_upload_plan", lambda _root: plan)

    def fail(*_args: object, **_kwargs: object) -> None:
        raise failure

    monkeypatch.setattr(publication_upload, "default_runner_with_retry", fail)

    exit_code = cli.run(
        [
            "publish",
            *_common_args(source_root, data_root),
            "--plan",
            "identity",
        ]
    )
    captured = capsys.readouterr()

    assert exit_code == 5  # publication failure
    assert captured.out == ""
    assert captured.err == f"error: {message}\n"
    assert "Traceback" not in captured.err


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
    monkeypatch.setattr(cli_handlers, "run_and_publish", lambda **_kwargs: _report())

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

    monkeypatch.setattr(cli_handlers, "discover_sources", fail_discovery)

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

    monkeypatch.setattr(cli_handlers, "discover_sources", interrupt_discovery)

    exit_code = cli.run(_inspect_args(tmp_path / "raw", tmp_path / "generated"))
    captured = capsys.readouterr()

    assert exit_code == 130
    assert captured.out == ""
    assert captured.err == ""


def _patch_real_orchestrator(monkeypatch: pytest.MonkeyPatch, source_root: Path) -> None:
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

    monkeypatch.setattr(cli_handlers, "run_and_publish", run_real_orchestrator)


def _run_and_publish(flags: list[str], source_root: Path, data_root: Path) -> int:
    return cli.run(
        [
            *flags,
            "run-and-publish",
            *_common_args(source_root, data_root),
            "--confirm-repo",
            "NoeFlandre/osm-polygon-description-tag",
        ]
    )


def test_noninteractive_run_and_publish_keeps_progress_plain(
    monkeypatch: pytest.MonkeyPatch,
    cli_roots: tuple[Path, Path],
    capsys: pytest.CaptureFixture[str],
) -> None:
    source_root, data_root = cli_roots
    _patch_real_orchestrator(monkeypatch, source_root)

    exit_code = _run_and_publish([], source_root, data_root)
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


@pytest.mark.parametrize(
    ("flags", "info", "debug"),
    [([], True, False), (["-v"], True, True), (["-q"], False, False)],
)
def test_verbosity_flags_filter_the_human_event_lines(
    monkeypatch: pytest.MonkeyPatch,
    cli_roots: tuple[Path, Path],
    capsys: pytest.CaptureFixture[str],
    flags: list[str],
    info: bool,
    debug: bool,
) -> None:
    source_root, data_root = cli_roots
    _patch_real_orchestrator(monkeypatch, source_root)

    assert _run_and_publish(flags, source_root, data_root) == 0

    err = capsys.readouterr().err
    assert (" INFO " in err) is info
    assert (f" resolved_config source_root={source_root} data_root={data_root}" in err) is debug
    # The durable JSONL log keeps every event whatever the flags.
    log = (data_root / "logs" / "run-and-publish.jsonl").read_text(encoding="utf-8")
    events = {json.loads(line)["event"] for line in log.splitlines()}
    assert {"resolved_config", "build_progress", "run_summary"} <= events


_RUN_AND_PUBLISH_REPO = "NoeFlandre/osm-polygon-description-tag"


def _run_and_publish_argv(cli_roots: tuple[Path, Path]) -> list[str]:
    source_root, data_root = cli_roots
    return [
        "run-and-publish",
        *_common_args(source_root, data_root),
        "--confirm-repo",
        _RUN_AND_PUBLISH_REPO,
    ]


def _stub_run_and_publish(monkeypatch: pytest.MonkeyPatch, error: Exception | None = None) -> None:
    """Log one INFO event through the real logger, then raise ``error`` when given."""

    def fake_run_and_publish(**kwargs: Any) -> SimpleNamespace:
        kwargs["logger"].event("run_summary", source_count=1)
        if error is not None:
            raise error
        return SimpleNamespace(to_payload=lambda: {"source_count": 1})

    monkeypatch.setattr(cli_handlers, "run_and_publish", fake_run_and_publish)


def _app_invoke(argv: list[str]) -> None:
    """Call the Typer app object directly, as a test runner or library caller would."""
    cli.app(args=argv, prog_name="osm-polygon-description-tag", standalone_mode=False)


def _without_clock_and_run_id(stderr: str) -> str:
    """Keep level, event and fields: the timestamp and the random run id differ per call."""
    return re.sub(r"^\S+ (\w+) run=\S+ ", r"\1 ", stderr, flags=re.MULTILINE)


def _run_stderr(argv: list[str], capsys: pytest.CaptureFixture[str]) -> str:
    capsys.readouterr()
    assert cli.run(argv) == 0
    return _without_clock_and_run_id(capsys.readouterr().err)


def _handler_stderr(cli_roots: tuple[Path, Path], capsys: pytest.CaptureFixture[str]) -> str:
    """Call the run-and-publish handler with no verbosity flags, outside the group callback."""
    source_root, data_root = cli_roots
    capsys.readouterr()
    presenter = TerminalPresenter(stderr=sys.stderr)
    try:
        cli.handle_run_and_publish(
            RunAndPublishRequest(
                confirm_repo=_RUN_AND_PUBLISH_REPO,
                source_root=source_root,
                data_root=data_root,
                osmium="fake-osmium",
                presenter=presenter,
            )
        )
    finally:
        presenter.close()
    return _without_clock_and_run_id(capsys.readouterr().err)


def _start_from_plain_invocation(
    cli_roots: tuple[Path, Path], capsys: pytest.CaptureFixture[str]
) -> None:
    """Fix the starting level explicitly: a plain invocation leaves INFO, whatever ran before."""
    _run_stderr(_run_and_publish_argv(cli_roots), capsys)


def test_quiet_app_call_does_not_leak_into_a_later_command(
    monkeypatch: pytest.MonkeyPatch,
    cli_roots: tuple[Path, Path],
    capsys: pytest.CaptureFixture[str],
) -> None:
    _stub_run_and_publish(monkeypatch)
    _start_from_plain_invocation(cli_roots, capsys)
    baseline = _handler_stderr(cli_roots, capsys)
    assert "INFO run_summary" in baseline

    _app_invoke(["-q", *_run_and_publish_argv(cli_roots)])

    assert _handler_stderr(cli_roots, capsys) == baseline


def test_exception_through_app_call_leaves_no_verbosity_behind(
    monkeypatch: pytest.MonkeyPatch,
    cli_roots: tuple[Path, Path],
    capsys: pytest.CaptureFixture[str],
) -> None:
    _stub_run_and_publish(monkeypatch)
    _start_from_plain_invocation(cli_roots, capsys)
    baseline = _handler_stderr(cli_roots, capsys)

    _stub_run_and_publish(monkeypatch, error=OSError("simulated failure"))
    with pytest.raises(OSError, match="simulated failure"):
        _app_invoke(["-v", *_run_and_publish_argv(cli_roots)])

    _stub_run_and_publish(monkeypatch)
    assert _handler_stderr(cli_roots, capsys) == baseline


def test_exception_through_run_leaves_no_verbosity_behind(
    monkeypatch: pytest.MonkeyPatch,
    cli_roots: tuple[Path, Path],
    capsys: pytest.CaptureFixture[str],
) -> None:
    _stub_run_and_publish(monkeypatch)
    _start_from_plain_invocation(cli_roots, capsys)
    baseline = _handler_stderr(cli_roots, capsys)

    _stub_run_and_publish(monkeypatch, error=OSError("simulated failure"))
    assert cli.run(["-v", *_run_and_publish_argv(cli_roots)]) == 1

    _stub_run_and_publish(monkeypatch)
    assert _handler_stderr(cli_roots, capsys) == baseline


def test_interleaved_verbose_and_quiet_invocations_match_isolated_runs(
    monkeypatch: pytest.MonkeyPatch,
    cli_roots: tuple[Path, Path],
    capsys: pytest.CaptureFixture[str],
) -> None:
    argv = _run_and_publish_argv(cli_roots)
    _stub_run_and_publish(monkeypatch)
    _start_from_plain_invocation(cli_roots, capsys)
    baseline = _handler_stderr(cli_roots, capsys)
    alone = {flags: _run_stderr([*flags, *argv], capsys) for flags in (("-v",), ("-q",), ())}

    for flags in (("-v",), ("-q",), (), ("-q",), ("-v",), ()):
        _app_invoke([*flags, *argv])
        assert _handler_stderr(cli_roots, capsys) == baseline, f"handler after direct {flags}"
        assert _run_stderr([*flags, *argv], capsys) == alone[flags], f"run {flags}"


def test_repeated_invocations_give_the_same_output(
    monkeypatch: pytest.MonkeyPatch,
    cli_roots: tuple[Path, Path],
    capsys: pytest.CaptureFixture[str],
) -> None:
    argv = _run_and_publish_argv(cli_roots)
    _stub_run_and_publish(monkeypatch)
    _start_from_plain_invocation(cli_roots, capsys)
    baseline = _handler_stderr(cli_roots, capsys)
    first_quiet = _run_stderr(["-q", *argv], capsys)

    for _ in range(3):
        _app_invoke(["-v", *argv])
        assert _handler_stderr(cli_roots, capsys) == baseline
        assert _run_stderr(["-q", *argv], capsys) == first_quiet


def test_verbose_and_quiet_cannot_be_combined(capsys: pytest.CaptureFixture[str]) -> None:
    assert cli.run(["-v", "-q", "inspect"]) == 2
    assert "--verbose and --quiet cannot be combined" in capsys.readouterr().err
