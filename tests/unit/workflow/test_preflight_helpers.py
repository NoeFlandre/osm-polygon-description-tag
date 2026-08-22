"""Focused behavioral coverage for workflow preflight helpers."""

from __future__ import annotations

import subprocess
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

import osm_polygon_description_tag.workflow.preflight as preflight
from osm_polygon_description_tag.config import Paths
from osm_polygon_description_tag.publication.models import REPO_ID
from osm_polygon_description_tag.workflow.preflight import PreflightError


def _paths(tmp_path: Path) -> Paths:
    source_root = tmp_path / "raw"
    data_root = tmp_path / "generated"
    source_root.mkdir()
    data_root.mkdir()
    (source_root / "a.osm.pbf").write_bytes(b"source")
    return Paths(source_root=source_root, data_root=data_root)


def test_assert_osmium_output_accepts_each_supported_marker() -> None:
    preflight._assert_osmium_output("osmium", "libosmium 2.20")
    preflight._assert_osmium_output("osmium", "osmium version 1.19")


def test_assert_osmium_output_rejects_output_without_marker() -> None:
    with pytest.raises(PreflightError, match="does not look like a real osmium-tool binary"):
        preflight._assert_osmium_output("osmium", "version 1.19")


def test_probe_osmium_version_resolves_binary_validates_output_and_returns_first_line(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[tuple[str, str]] = []
    monkeypatch.setattr(preflight.shutil, "which", lambda executable: "/resolved/osmium")

    def run(binary: str, executable: str) -> str:
        calls.append((binary, executable))
        return "osmium version 1.19\nlibosmium 2.20\n"

    validated: list[tuple[str, str]] = []

    def validate(binary: str, output: str) -> None:
        validated.append((binary, output))
        assert binary == "/resolved/osmium"
        assert output.startswith("osmium version")

    monkeypatch.setattr(preflight, "_run_osmium_version", run)
    monkeypatch.setattr(preflight, "_assert_osmium_output", validate)

    assert preflight._probe_osmium_version("osmium") == "osmium version 1.19"
    assert calls == [("/resolved/osmium", "osmium")]
    assert validated == [("/resolved/osmium", "osmium version 1.19\nlibosmium 2.20\n")]


def test_probe_osmium_version_falls_back_to_requested_executable_and_handles_empty_output(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def no_binary(executable: str) -> None:
        assert executable == "custom-osmium"
        return None

    monkeypatch.setattr(preflight.shutil, "which", no_binary)
    calls: list[tuple[str, str]] = []

    def run(binary: str, executable: str) -> str:
        calls.append((binary, executable))
        return ""

    monkeypatch.setattr(preflight, "_run_osmium_version", run)
    monkeypatch.setattr(preflight, "_assert_osmium_output", lambda _binary, _output: None)

    assert preflight._probe_osmium_version("custom-osmium") == ""
    assert calls == [("custom-osmium", "custom-osmium")]


def test_run_osmium_version_uses_safe_text_capture_contract(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    seen: dict[str, Any] = {}

    def run(command: list[str], **kwargs: Any) -> SimpleNamespace:
        seen["command"] = command
        seen["kwargs"] = kwargs
        return SimpleNamespace(stdout="stdout\n", stderr="stderr\n")

    monkeypatch.setattr(preflight.subprocess, "run", run)

    assert preflight._run_osmium_version("/bin/osmium", "osmium") == "stdout\n"
    assert seen == {
        "command": ["/bin/osmium", "--version"],
        "kwargs": {
            "check": True,
            "shell": False,
            "capture_output": True,
            "text": True,
            "timeout": 15,
        },
    }


@pytest.mark.parametrize(
    ("stdout", "stderr", "expected"),
    [("", "stderr\n", "stderr\n"), ("", "", "")],
)
def test_run_osmium_version_uses_stderr_then_empty_fallback(
    monkeypatch: pytest.MonkeyPatch,
    stdout: str,
    stderr: str,
    expected: str,
) -> None:
    monkeypatch.setattr(
        preflight.subprocess,
        "run",
        lambda *_args, **_kwargs: SimpleNamespace(stdout=stdout, stderr=stderr),
    )

    assert preflight._run_osmium_version("osmium", "osmium") == expected


def test_run_osmium_version_prefers_stdout_when_both_streams_are_present(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        preflight.subprocess,
        "run",
        lambda *_args, **_kwargs: SimpleNamespace(stdout="stdout", stderr="stderr"),
    )

    assert preflight._run_osmium_version("osmium", "osmium") == "stdout"


@pytest.mark.parametrize(
    "error",
    [
        FileNotFoundError("missing"),
        subprocess.CalledProcessError(1, ["osmium", "--version"]),
    ],
)
def test_run_osmium_version_wraps_subprocess_failures(
    monkeypatch: pytest.MonkeyPatch,
    error: Exception,
) -> None:
    def fail(*_args: Any, **_kwargs: Any) -> None:
        raise error

    monkeypatch.setattr(preflight.subprocess, "run", fail)

    with pytest.raises(PreflightError, match=r"osmium --version failed for custom-osmium"):
        preflight._run_osmium_version("osmium", "custom-osmium")


def test_hf_cli_identity_uses_exact_auth_command_and_subprocess_contract(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    seen: dict[str, Any] = {}

    def run(command: list[str], **kwargs: Any) -> SimpleNamespace:
        seen["command"] = command
        seen["kwargs"] = kwargs
        return SimpleNamespace(stdout=" first-user \nsecond-user\n")

    monkeypatch.setattr(preflight.subprocess, "run", run)

    assert preflight._hf_cli_identity("/bin/hf") == "first-user"
    assert seen == {
        "command": ["/bin/hf", "auth", "whoami"],
        "kwargs": {
            "check": True,
            "shell": False,
            "capture_output": True,
            "text": True,
            "timeout": 15,
        },
    }


def test_hf_cli_identity_returns_empty_for_empty_stdout(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        preflight.subprocess,
        "run",
        lambda *_args, **_kwargs: SimpleNamespace(stdout=""),
    )

    assert preflight._hf_cli_identity("hf") == ""


@pytest.mark.parametrize(
    "error", [subprocess.CalledProcessError(1, ["hf"]), subprocess.TimeoutExpired("hf", 15)]
)
def test_hf_cli_identity_wraps_cli_failures(
    monkeypatch: pytest.MonkeyPatch,
    error: Exception,
) -> None:
    def fail(*_args: Any, **_kwargs: Any) -> None:
        raise error

    monkeypatch.setattr(preflight.subprocess, "run", fail)

    with pytest.raises(PreflightError, match="hf authentication check failed"):
        preflight._hf_cli_identity("hf")


def test_validate_local_prerequisites_reports_path_validation_failure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    paths = SimpleNamespace(validate=lambda: (_ for _ in ()).throw(ValueError("bad roots")))

    with pytest.raises(PreflightError, match=r"^path validation failed: bad roots$"):
        preflight._validate_local_prerequisites(
            paths,
            confirm_repo=REPO_ID,
            osmium_executable="osmium",
            hf_executable="hf",
        )


def test_validate_local_prerequisites_reports_missing_osmium_exactly(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(preflight.shutil, "which", lambda _executable: None)

    with pytest.raises(PreflightError, match=r"^osmium executable not found: custom-osmium$"):
        preflight._validate_local_prerequisites(
            SimpleNamespace(validate=lambda: None),
            confirm_repo=REPO_ID,
            osmium_executable="custom-osmium",
            hf_executable="hf",
        )


def test_validate_data_roots_reports_exact_boundary_errors(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    paths = _paths(tmp_path)
    monkeypatch.setattr(preflight.os, "access", lambda _path, _mode: False)

    with pytest.raises(PreflightError, match=r"^source root is not readable: .*/raw$"):
        preflight._validate_data_roots(paths)

    monkeypatch.setattr(preflight.os, "access", lambda path, mode: mode == preflight.os.R_OK)
    with pytest.raises(PreflightError, match=r"^data root is not writable: .*/generated$"):
        preflight._validate_data_roots(paths)


def test_validate_data_roots_reports_empty_source_discovery_exactly(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    paths = _paths(tmp_path)
    monkeypatch.setattr(preflight.os, "access", lambda _path, _mode: True)
    monkeypatch.setattr(preflight, "discover_sources", lambda _root: ())

    with pytest.raises(
        PreflightError, match=r"^no source PBF files found in .*/raw; nothing to publish$"
    ):
        preflight._validate_data_roots(paths)


def test_validate_data_roots_returns_discovered_sources(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    paths = _paths(tmp_path)
    sources = (SimpleNamespace(name="a"), SimpleNamespace(name="b"))
    seen: list[Path] = []
    monkeypatch.setattr(preflight.os, "access", lambda _path, _mode: True)
    monkeypatch.setattr(preflight, "discover_sources", lambda root: seen.append(root) or sources)

    assert preflight._validate_data_roots(paths) == sources
    assert seen == [paths.source_root]


def test_hub_identity_passes_exact_dataset_repository_arguments(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[tuple[str, str, str]] = []

    class StrictApi:
        def whoami(self) -> str:
            calls.append(("whoami", "", ""))
            return "hub-user"

        def repo_info(self, repo_id: str, *, repo_type: str) -> SimpleNamespace:
            calls.append(("repo_info", repo_id, repo_type))
            return SimpleNamespace(sha="remote-sha")

    monkeypatch.setattr(preflight._huggingface_hub, "HfApi", StrictApi)

    api, identity = preflight._hub_identity()

    assert isinstance(api, StrictApi)
    assert identity == ("hub-user", "remote-sha")
    assert calls == [("whoami", "", ""), ("repo_info", REPO_ID, "dataset")]


def test_hub_identity_reports_empty_identity_and_missing_sha_exactly(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class EmptyIdentityApi:
        def whoami(self) -> dict[str, str]:
            return {}

        def repo_info(self, *_args: Any, **_kwargs: Any) -> SimpleNamespace:
            return SimpleNamespace(sha="sha")

    monkeypatch.setattr(preflight._huggingface_hub, "HfApi", EmptyIdentityApi)
    with pytest.raises(PreflightError, match=r"^Hub identity is empty; check HF_TOKEN$"):
        preflight._hub_identity()

    class MissingShaApi:
        def whoami(self) -> dict[str, str]:
            return {"name": "hub-user"}

        def repo_info(self, *_args: Any, **_kwargs: Any) -> SimpleNamespace:
            return SimpleNamespace()

    monkeypatch.setattr(preflight._huggingface_hub, "HfApi", MissingShaApi)
    with pytest.raises(PreflightError, match=r"^Hub repository .+ returned no commit SHA$"):
        preflight._hub_identity()


def test_validate_hub_write_requires_dataset_and_write_true() -> None:
    calls: list[tuple[str, str, bool]] = []

    class StrictApi:
        def auth_check(self, repo_id: str, *, repo_type: str, write: bool) -> None:
            calls.append((repo_id, repo_type, write))

    preflight._validate_hub_write(StrictApi())
    assert calls == [(REPO_ID, "dataset", True)]


def test_validate_hub_write_wraps_permission_failure() -> None:
    class DeniedApi:
        def auth_check(self, *_args: Any, **_kwargs: Any) -> None:
            raise PermissionError("denied")

    with pytest.raises(PreflightError, match=r"^Hub write permission denied for .+: denied$"):
        preflight._validate_hub_write(DeniedApi())


def test_default_preflight_returns_complete_stable_report(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    paths = _paths(tmp_path)
    api = object()
    monkeypatch.setattr(
        preflight,
        "_validate_local_prerequisites",
        lambda *_args, **_kwargs: ("osmium version 1.19", "/bin/hf"),
    )
    monkeypatch.setattr(preflight, "_hf_cli_identity", lambda _resolved_hf: "cli-user")
    monkeypatch.setattr(
        preflight,
        "_validate_data_roots",
        lambda _paths: (SimpleNamespace(name="a"), SimpleNamespace(name="b")),
    )
    monkeypatch.setattr(
        preflight,
        "_hub_identity",
        lambda: (api, ("hub-user", "remote-sha")),
    )
    write_checks: list[object] = []
    monkeypatch.setattr(preflight, "_validate_hub_write", write_checks.append)
    monkeypatch.setattr(preflight, "osmium_export_config", lambda: Path("/config/osmium.json"))
    monkeypatch.setattr(preflight, "dataset_card_template", lambda: Path("/templates/card.md"))
    monkeypatch.setattr(preflight, "current_area_policy_sha256", lambda: "area-sha")

    report = preflight.default_preflight(
        paths,
        confirm_repo=REPO_ID,
        osmium_executable="osmium",
        hf_executable="hf",
    )

    assert report == {
        "osmium_executable": "osmium",
        "osmium_version": "osmium version 1.19",
        "hf_executable": "/bin/hf",
        "hf_whoami": "cli-user",
        "hf_identity": "hub-user",
        "hub_repo_sha": "remote-sha",
        "source_root": str(paths.source_root),
        "data_root": str(paths.data_root),
        "export_config": "/config/osmium.json",
        "card_template": "/templates/card.md",
        "repo_id": REPO_ID,
        "confirm_repo": REPO_ID,
        "source_count": 2,
        "transform_algorithm_version": preflight.TRANSFORM_ALGORITHM_VERSION,
        "area_policy_sha256": "area-sha",
    }
    assert write_checks == [api]
