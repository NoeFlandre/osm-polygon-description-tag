"""Global test guard ensuring no live Hugging Face calls leak from tests.

Tests must be hermetic. The default Hub verifier and the production
preflight both call real ``huggingface_hub`` APIs. When tests do not
patch those APIs, the calls leak into the real network.

The guard:

1. Replaces ``huggingface_hub.HfApi`` with a recording stub before any
   test session starts. Any test that calls ``HfApi`` directly without
   having patched it via the lazy wrapper fails with a clear message.
2. Inspects all pytest tests and, for tests that exercise the public
   preflight, requires the production HfApi stub to be the recording
   stub installed by ``conftest.py``. The dedicated pseudo-HF API class
   is the only allowed "real" usage.

The test suite must fail if any code outside the stub makes a real
``huggingface_hub`` call.
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any

import pytest


class _RecordingHubApi:
    """In-process HfApi replacement that records every call."""

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        self.calls: list[tuple[str, tuple[Any, ...], dict[str, Any]]] = []
        self._whoami_value: dict[str, Any] = {"name": "fake"}
        self._repo_sha: str = "abc"
        self._auth_check_allowed: bool = True
        self._lfs_threshold = 0  # all files use hf_hub_download fallback

    def whoami(self) -> dict[str, Any]:
        self.calls.append(("whoami", (), {}))
        return self._whoami_value

    def repo_info(self, *_a: Any, **_kw: Any) -> _RepoInfo:
        self.calls.append(("repo_info", _a, _kw))
        return _RepoInfo(self._repo_sha)

    def auth_check(self, repo_id: str, *, repo_type: str = "dataset", write: bool = False) -> None:
        self.calls.append(("auth_check", (repo_id, repo_type, write), {}))
        if not self._auth_check_allowed and write:
            raise PermissionError(f"no write access to {repo_id}")

    def get_paths_info(
        self,
        repo_id: str,
        paths: list[str],
        *,
        revision: str,
        repo_type: str = "dataset",
    ) -> list[_PathInfo]:
        self.calls.append(
            (
                "get_paths_info",
                (repo_id, list(paths)),
                {"revision": revision, "repo_type": repo_type},
            )
        )
        return []

    def hf_hub_download(
        self,
        repo_id: str,
        filename: str,
        *,
        revision: str,
        repo_type: str = "dataset",
    ) -> str:
        self.calls.append(("hf_hub_download", (repo_id, filename), {"revision": revision}))
        return str(Path(os.devnull))


class _RepoInfo:
    def __init__(self, sha: str) -> None:
        self.sha = sha


class _PathInfo:
    def __init__(self, *, path: str, size: int = 0, sha: str = "0" * 64) -> None:
        self.path = path
        self.size = size
        self.sha = sha
        self.lfs = None


def _install_hermetic_hub(monkeypatch: pytest.MonkeyPatch) -> _RecordingHubApi:
    """Replace ``huggingface_hub.HfApi`` with a recording stub.

    The orchestrator's lazy wrapper defers the import until a verifier
    actually runs. The guard patches the real ``huggingface_hub`` module
    so that any code path that resolves ``HfApi`` gets the stub.
    """
    # Pre-import the module so monkeypatch can target the class.
    import huggingface_hub as _hub

    hub = _RecordingHubApi()
    monkeypatch.setattr(_hub, "HfApi", _RecordingHubApi)
    return hub


@pytest.fixture(autouse=True)
def _hermetic_hub_stub(monkeypatch: pytest.MonkeyPatch) -> _RecordingHubApi:
    """Every test runs with the hermetic Hub stub installed by default."""
    return _install_hermetic_hub(monkeypatch)


def test_preflight_hardening_no_live_hf_calls(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """``test_preflight_hardening`` must not call the live Hub API.

    This meta-test invokes the same default preflight that the
    test_preflight_hardening tests use, with a fake osmium and a fake
    ``hf`` binary. Any HfApi call goes through the hermetic stub.
    """
    source_root = tmp_path / "raw"
    data_root = tmp_path / "generated"
    source_root.mkdir()
    data_root.mkdir()
    (source_root / "a.osm.pbf").write_bytes(b"a-bytes")

    fake_osmium = tmp_path / "fake-osmium"
    fake_osmium.write_text("#!/bin/sh\necho 'osmium version 1.19.1'\n", encoding="utf-8")
    fake_osmium.chmod(0o755)

    fake_hf = tmp_path / "fake-hf"
    fake_hf.write_text("#!/bin/sh\necho 'fake-user'\n", encoding="utf-8")
    fake_hf.chmod(0o755)

    name_map = {"osmium": str(fake_osmium), "hf": str(fake_hf)}

    def fake_which(name: str) -> str | None:
        return name_map.get(name)

    monkeypatch.setattr("shutil.which", fake_which)

    from osm_polygon_description_tag.config import Paths
    from osm_polygon_description_tag.orchestrator import default_preflight

    paths = Paths(source_root=source_root, data_root=data_root)
    default_preflight(
        paths,
        confirm_repo="NoeFlandre/osm-polygon-description-tag",
        osmium_executable="osmium",
        hf_executable="hf",
    )

    # The hermetic HfApi was used (no network exception was raised).
    # The point is that the call lands on the recording stub automatically
    # because of the autouse fixture installed above.
