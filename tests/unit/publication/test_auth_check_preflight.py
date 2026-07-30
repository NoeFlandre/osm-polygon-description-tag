"""RED tests proving preflight verifies Hub write permission.

The current ``whoami()`` + ``repo_info()`` only establishes identity and
read access. The Hub API's non-mutating write permission check must be
called before any PBF is opened or any generated artifact is created.

Specifically:

  api.auth_check(
      "NoeFlandre/osm-polygon-description-tag",
      repo_type="dataset",
      write=True,
  )

Tests must prove:

- ``write=True`` and ``repo_type="dataset"`` are passed to ``auth_check``.
- A denied write permission raises :class:`PreflightError`.
- No exporter is invoked, no generator is invoked, no uploader is invoked,
  and no state mutation occurs after the permission denial.
"""

from __future__ import annotations

import subprocess
from pathlib import Path
from typing import Any

import pytest

from osm_polygon_description_tag.config import Paths
from osm_polygon_description_tag.orchestrator import (
    PreflightError,
    default_preflight,
)


def _paths(tmp_path: Path) -> Paths:
    source_root = tmp_path / "raw"
    data_root = tmp_path / "generated"
    source_root.mkdir()
    data_root.mkdir()
    (source_root / "a.osm.pbf").write_bytes(b"a-bytes")
    return Paths(source_root=source_root, data_root=data_root)


class _RecordingHubApi:
    """In-process HfApi replacement that records every call."""

    def __init__(self, *, allow_write: bool = True):
        self.calls: list[tuple[str, tuple, dict]] = []
        self._allow_write = allow_write

    def whoami(self) -> object:
        self.calls.append(("whoami", (), {}))
        return {"name": "fake-user"}

    def repo_info(self, *_a: object, **_kw: object) -> object:
        self.calls.append(("repo_info", _a, _kw))
        info = _Info()
        return info

    def auth_check(self, repo_id: str, *, repo_type: str = "dataset", write: bool = False) -> None:
        self.calls.append(("auth_check", (repo_id, repo_type, write), {}))
        if not self._allow_write and write:
            raise PermissionError(f"no write access to {repo_id}")


class _Info:
    sha = "abc"


def _patch_hf(monkeypatch: pytest.MonkeyPatch, hub: _RecordingHubApi) -> None:
    """Patch the lazy HfApi to use an in-process stand-in."""
    import osm_polygon_description_tag.workflow.preflight as orch

    def factory(*_a: object, **_kw: object) -> _RecordingHubApi:
        return hub

    monkeypatch.setattr(orch._huggingface_hub, "HfApi", factory)


def _patch_osmium(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Path:
    """Install a fake osmium binary that reports a real-ish version."""
    fake = tmp_path / "fake-osmium"
    fake.write_text("#!/bin/sh\necho 'osmium version 1.19.1'\n", encoding="utf-8")
    fake.chmod(0o755)
    return fake


def _patch_hf_binary(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Path:
    """Install a fake ``hf`` binary that reports a fake user."""
    fake = tmp_path / "fake-hf"
    fake.write_text("#!/bin/sh\necho 'fake-user'\n", encoding="utf-8")
    fake.chmod(0o755)
    return fake


def _patch_which(monkeypatch: pytest.MonkeyPatch, *binaries: Path) -> None:
    """Make ``shutil.which`` resolve the provided binaries by name."""
    name_map = {binary.name.replace("fake-", ""): str(binary) for binary in binaries}

    def fake_which(name: str) -> str | None:
        return name_map.get(name)

    monkeypatch.setattr("shutil.which", fake_which)


def test_preflight_calls_auth_check_with_write_true(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """The preflight must call ``auth_check(write=True, repo_type='dataset')``."""
    paths = _paths(tmp_path)
    hub = _RecordingHubApi(allow_write=True)
    _patch_hf(monkeypatch, hub)
    osmium = _patch_osmium(monkeypatch, tmp_path)
    hf_bin = _patch_hf_binary(monkeypatch, tmp_path)
    _patch_which(monkeypatch, osmium, hf_bin)
    commands: list[list[str]] = []
    real_run = subprocess.run

    def recording_run(command: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
        commands.append(command)
        return real_run(command, **kwargs)

    monkeypatch.setattr(
        "osm_polygon_description_tag.workflow.preflight.subprocess.run", recording_run
    )

    report = default_preflight(
        paths,
        confirm_repo="NoeFlandre/osm-polygon-description-tag",
        osmium_executable="osmium",
        hf_executable="hf",
    )

    auth_check_calls = [call for call in hub.calls if call[0] == "auth_check"]
    assert auth_check_calls, "auth_check was never called"
    _name, args, _kw = auth_check_calls[0]
    repo_id, repo_type, write = args
    assert repo_id == "NoeFlandre/osm-polygon-description-tag"
    assert repo_type == "dataset"
    assert write is True
    assert [str(hf_bin), "auth", "whoami"] in commands
    assert ["hf", "auth", "whoami"] not in commands
    assert report["hf_executable"] == str(hf_bin)


def test_preflight_never_falls_back_to_installed_hf(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    paths = _paths(tmp_path)
    osmium = _patch_osmium(monkeypatch, tmp_path)
    monkeypatch.setattr(
        "osm_polygon_description_tag.workflow.preflight.shutil.which",
        lambda name: str(osmium) if name == "osmium" else None,
    )
    launched: list[list[str]] = []

    def forbidden_run(command: list[str], **_kwargs: object) -> None:
        launched.append(command)
        raise AssertionError("no subprocess may launch when hf resolution fails")

    monkeypatch.setattr(
        "osm_polygon_description_tag.workflow.preflight.subprocess.run", forbidden_run
    )

    with pytest.raises(PreflightError, match="hf executable not found"):
        default_preflight(
            paths,
            confirm_repo="NoeFlandre/osm-polygon-description-tag",
            osmium_executable="osmium",
            hf_executable="hf",
        )

    assert launched == []


def test_preflight_denied_write_raises_preflight_error(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """A denied write permission must raise PreflightError."""
    paths = _paths(tmp_path)
    hub = _RecordingHubApi(allow_write=False)
    _patch_hf(monkeypatch, hub)
    osmium = _patch_osmium(monkeypatch, tmp_path)
    hf_bin = _patch_hf_binary(monkeypatch, tmp_path)
    _patch_which(monkeypatch, osmium, hf_bin)

    with pytest.raises(PreflightError, match="write"):
        default_preflight(
            paths,
            confirm_repo="NoeFlandre/osm-polygon-description-tag",
            osmium_executable="osmium",
            hf_executable="hf",
        )


def test_preflight_denial_prevents_any_subprocess_or_filesystem_mutation(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """After preflight denial, no exporter / uploader / mutation may run."""
    from osm_polygon_description_tag.cli import run as cli_run
    from osm_polygon_description_tag.publication import REPO_ID

    paths = _paths(tmp_path)
    hub = _RecordingHubApi(allow_write=False)
    _patch_hf(monkeypatch, hub)
    osmium = _patch_osmium(monkeypatch, tmp_path)
    hf_bin = _patch_hf_binary(monkeypatch, tmp_path)
    _patch_which(monkeypatch, osmium, hf_bin)

    # If any subsequent code attempts to invoke the exporter or uploader,
    # raise here so the test fails loudly.
    build_calls = 0

    def forbidden(*_a: Any, **_kw: Any) -> None:
        nonlocal build_calls
        build_calls += 1
        raise AssertionError("must not be called after preflight denial")

    monkeypatch.setattr(
        "osm_polygon_description_tag.workflow.orchestrator.build_one", forbidden
    )
    monkeypatch.setattr(
        "osm_polygon_description_tag.publication.upload._default_runner_with_retry", forbidden
    )

    # Snapshot the data root before invocation.
    data_root = paths.data_root
    files_before = sorted(p.name for p in data_root.iterdir())

    exit_code = cli_run(
        [
            "run-and-publish",
            "--source-root",
            str(paths.source_root),
            "--data-root",
            str(paths.data_root),
            "--confirm-repo",
            REPO_ID,
        ]
    )
    assert exit_code != 0
    assert build_calls == 0

    # Prove the sentinel is installed at the live orchestration boundary.
    import osm_polygon_description_tag.workflow.orchestrator as workflow_orchestrator

    with pytest.raises(AssertionError, match="must not be called"):
        workflow_orchestrator.build_one()

    # No PBF or generated artifact file was created or removed in the data root.
    files_after = sorted(p.name for p in data_root.iterdir())
    new_dirs = sorted(set(files_after) - set(files_before))
    assert new_dirs == [], f"unexpected new entries: {new_dirs}"
    # No publication state was written.
    assert not (data_root / "publication-state.json").exists()
    # The orchestrator failed because of preflight denial.
    expected_calls = [
        "whoami",
        "repo_info",
        "auth_check",
    ]
    assert [call[0] for call in hub.calls] == expected_calls
