"""Acceptance: an interrupted multi-source publication resumes byte-for-byte.

User story: as an operator, I can stop a run after source work is complete,
restart it against the same roots, and get the same artifacts as an uninterrupted
run without rebuilding sources that already have valid outputs.
"""

from __future__ import annotations

import json
import shutil
from pathlib import Path

import pytest

from osm_polygon_description_tag.cli import run as cli_run
from osm_polygon_description_tag.dataset.manifest import file_sha256
from osm_polygon_description_tag.publication import REPO_ID
from osm_polygon_description_tag.workflow.orchestrator import PUBLICATION_STATE_FILENAME
from tests.helpers.osmium import real_osmium_path, write_pbf

pytestmark = pytest.mark.acceptance

FIXTURE = Path("tests/fixtures/descriptions.osm")
_CLOCK = "2026-07-28T00:00:00+00:00"


def _counting_osmium(
    tmp_path: Path, executable: str, monkeypatch: pytest.MonkeyPatch
) -> tuple[str, Path]:
    trace = tmp_path / "osmium-commands.txt"
    wrapper = tmp_path / "osmium-wrapper"
    wrapper.write_text(
        '#!/bin/sh\nprintf \'%s\\n\' "$*" >> "$OSM_TEST_TRACE"\n'
        'exec "$OSM_TEST_REAL_OSMIUM" "$@"\n',
        encoding="utf-8",
    )
    wrapper.chmod(0o755)
    monkeypatch.setenv("OSM_TEST_TRACE", str(trace))
    monkeypatch.setenv("OSM_TEST_REAL_OSMIUM", executable)
    return str(wrapper), trace


def _source_roots(tmp_path: Path, executable: str) -> tuple[Path, Path, Path, Path]:
    seed = tmp_path / "seed.osm.pbf"
    write_pbf(executable, FIXTURE, seed)

    interrupted_source = tmp_path / "interrupted" / "raw"
    interrupted_data = tmp_path / "interrupted" / "generated"
    control_source = tmp_path / "control" / "raw"
    control_data = tmp_path / "control" / "generated"
    for source_root, data_root in (
        (interrupted_source, interrupted_data),
        (control_source, control_data),
    ):
        source_root.mkdir(parents=True)
        data_root.mkdir(parents=True)
        for name in ("a.osm.pbf", "b.osm.pbf"):
            shutil.copy2(seed, source_root / name)
    return interrupted_source, interrupted_data, control_source, control_data


def _install_hub_fakes(monkeypatch: pytest.MonkeyPatch) -> list[list[str]]:
    import osm_polygon_description_tag.publication.upload as upload_module
    from osm_polygon_description_tag.workflow import orchestrator

    pbf_uploads = 0
    interrupt_second_pbf = True
    uploaded_commands: list[list[str]] = []

    def upload_runner(command: list[str], timeout: float | None = None) -> str:
        nonlocal pbf_uploads, interrupt_second_pbf
        includes = [
            command[index + 1] for index, value in enumerate(command) if value == "--include"
        ]
        if any(item.startswith("data/") for item in includes):
            pbf_uploads += 1
            if interrupt_second_pbf and pbf_uploads == 2:
                interrupt_second_pbf = False
                raise KeyboardInterrupt
        uploaded_commands.append(list(command))
        return "fake-upload-revision"

    def verifier_factory():
        return lambda _repo_id, _files: "fake-verified-revision"

    monkeypatch.setattr(upload_module, "default_runner_with_retry", upload_runner)
    monkeypatch.setattr(orchestrator, "default_hub_verifier_factory", verifier_factory)
    monkeypatch.setattr(orchestrator, "_default_clock", lambda: _CLOCK)
    return uploaded_commands


def _cli_run(source_root: Path, data_root: Path, osmium: str) -> int:
    return cli_run(
        [
            "run-and-publish",
            "--source-root",
            str(source_root),
            "--data-root",
            str(data_root),
            "--confirm-repo",
            REPO_ID,
            "--osmium",
            osmium,
        ]
    )


def _parquet_and_manifest_bytes(data_root: Path) -> dict[str, bytes]:
    return {
        path.relative_to(data_root).as_posix(): path.read_bytes()
        for pattern in ("data/*.parquet", "manifests/*.manifest.json")
        for path in sorted(data_root.glob(pattern))
    }


def _export_calls(trace: Path) -> tuple[str, ...]:
    if not trace.exists():
        return ()
    return tuple(
        line
        for line in trace.read_text(encoding="utf-8").splitlines()
        if line.startswith("export ")
    )


def _assert_manifest_hashes_match_outputs(data_root: Path, outputs: dict[str, bytes]) -> None:
    for relative_path, payload in outputs.items():
        if relative_path.startswith("manifests/"):
            output_name = Path(relative_path).name.removesuffix(".manifest.json")
            parquet_path = data_root / "data" / f"{output_name}.parquet"
            manifest = json.loads(payload)
            assert manifest["output"]["sha256"] == file_sha256(parquet_path)


def test_given_an_interrupted_multi_source_run_when_resumed_then_outputs_match_uninterrupted(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Resume in place and compare both source artifacts with an uninterrupted control."""
    real_osmium = real_osmium_path()
    wrapper, trace = _counting_osmium(tmp_path, real_osmium, monkeypatch)
    interrupted_source, interrupted_data, control_source, control_data = _source_roots(
        tmp_path, real_osmium
    )
    uploaded_commands = _install_hub_fakes(monkeypatch)

    interrupted_code = _cli_run(interrupted_source, interrupted_data, wrapper)
    assert interrupted_code == 130
    capsys.readouterr()
    state = json.loads((interrupted_data / PUBLICATION_STATE_FILENAME).read_text(encoding="utf-8"))
    assert set(state["published"]) == {"a.osm.pbf"}
    interrupted_artifacts = _parquet_and_manifest_bytes(interrupted_data)
    assert set(interrupted_artifacts) == {
        "data/a.parquet",
        "data/b.parquet",
        "manifests/a.manifest.json",
        "manifests/b.manifest.json",
    }
    exports_before_resume = _export_calls(trace)
    assert len(exports_before_resume) == 2
    assert any("a.osm.pbf" in command for command in exports_before_resume)
    assert any("b.osm.pbf" in command for command in exports_before_resume)

    resumed_code = _cli_run(interrupted_source, interrupted_data, wrapper)
    resumed_output = capsys.readouterr().out
    assert resumed_code == 0
    assert '"status": "already-published"' in resumed_output
    assert '"status": "reused-local-needs-upload"' in resumed_output
    assert _export_calls(trace) == exports_before_resume, "resume re-exported a completed source"
    assert _parquet_and_manifest_bytes(interrupted_data) == interrupted_artifacts
    state = json.loads((interrupted_data / PUBLICATION_STATE_FILENAME).read_text(encoding="utf-8"))
    assert set(state["published"]) == {"a.osm.pbf", "b.osm.pbf"}

    control_code = _cli_run(control_source, control_data, wrapper)
    capsys.readouterr()
    assert control_code == 0
    assert len(_export_calls(trace)) == 4
    resumed_outputs = _parquet_and_manifest_bytes(interrupted_data)
    control_outputs = _parquet_and_manifest_bytes(control_data)
    assert resumed_outputs == control_outputs
    _assert_manifest_hashes_match_outputs(interrupted_data, resumed_outputs)
    assert any("data/b.parquet" in command for command in uploaded_commands)
