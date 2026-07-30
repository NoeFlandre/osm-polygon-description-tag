"""RED tests for the real Hugging Face Hub verifier.

The default Hub verifier wraps :class:`huggingface_hub.HfApi` and confirms:

1. ``whoami`` returns a non-empty user identity (auth token works).
2. ``repo_info(repo_id)`` reports the current commit SHA.
3. Each :class:`UploadItem` in the plan is fetched or listed from the
   repository at that exact SHA.
4. The local SHA-256 matches what the Hub reports as the file's digest.
   For LFS files (sized >~5MB typically) the ``sha256`` field of the LFS
   metadata block is used. For non-LFS files the file content is read and
   hashed.

The verifier returns the real, verified commit SHA.
"""

from __future__ import annotations

import hashlib
from typing import Any

import pytest

from osm_polygon_description_tag.orchestrator import default_hub_verifier_factory
from osm_polygon_description_tag.publication import REPO_ID, UploadItem


class _FakeHubApi:
    """In-process stand-in for ``huggingface_hub.HfApi``.

    Stores path -> content mapping so the verifier's hash-after-download
    path can reproduce any SHA deterministically.
    """

    def __init__(
        self, *, repo_sha: str = "0123456789abcdef", files: dict[str, bytes] | None = None
    ):
        self._repo_sha = repo_sha
        self._files = files or {}
        self.calls: list[tuple[str, tuple]] = []
        self._whoami_raises: BaseException | None = None

    def fail_whoami(self, exc: BaseException) -> None:
        self._whoami_raises = exc

    def whoami(self) -> object:
        self.calls.append(("whoami", ()))
        if self._whoami_raises is not None:
            raise self._whoami_raises
        return {"name": "test-user"}

    def repo_info(self, repo_id: str, *, repo_type: str = "dataset"):
        self.calls.append(("repo_info", (repo_id, repo_type)))
        return _RepoInfo(self._repo_sha)

    def list_repo_files(self, repo_id: str, *, revision: str, repo_type: str = "dataset"):
        self.calls.append(("list_repo_files", (repo_id, revision, repo_type)))
        return list(self._files)

    def get_paths_info(self, repo_id: str, paths, *, revision: str, repo_type: str = "dataset"):
        self.calls.append(("get_paths_info", (repo_id, list(paths), revision, repo_type)))
        out: list[_PathInfo] = []
        for path in paths:
            content = self._files.get(path)
            if content is None:
                continue
            out.append(
                _PathInfo(path=path, size=len(content), sha=hashlib.sha256(content).hexdigest())
            )
        return out

    def get_hf_file_metadata(
        self, repo_id: str, filename: str, *, revision: str, repo_type: str = "dataset"
    ):
        self.calls.append(("get_hf_file_metadata", (repo_id, filename, revision, repo_type)))
        content = self._files.get(filename)
        if content is None:
            raise LookupError(filename)
        return _RepoFileInfo(size=len(content), lfs_sha=None)

    def hf_hub_download(
        self,
        repo_id: str,
        filename: str,
        *,
        revision: str,
        repo_type: str = "dataset",
        **kwargs: Any,
    ) -> str:
        self.calls.append(("hf_hub_download", (repo_id, filename, revision, repo_type)))
        content = self._files.get(filename)
        if content is None:
            raise LookupError(filename)
        import os as _os
        import tempfile

        handle, path_str = tempfile.mkstemp(prefix="hf-", suffix=f"-{_os.path.basename(filename)}")
        _ = handle
        from pathlib import Path as _Path

        _Path(path_str).write_bytes(content)
        return path_str


class _RepoInfo:
    def __init__(self, sha: str):
        self.sha = sha


class _PathInfo:
    def __init__(self, *, path: str, size: int, sha: str):
        self.path = path
        self.size = size
        self.lfs = None
        self.sha256 = sha


class _RepoFileInfo:
    def __init__(self, *, size: int, lfs_sha: str | None):
        self.size = size
        self.lfs = _LfsInfo(lfs_sha) if lfs_sha else None


class _LfsInfo:
    def __init__(self, sha: str):
        self.sha256 = sha


@pytest.fixture
def patched_hf(monkeypatch: pytest.MonkeyPatch) -> dict[str, Any]:
    """Replace ``HfApi`` reference inside the orchestrator module."""
    import osm_polygon_description_tag.publication.verification as orch

    state: dict[str, Any] = {"api": _FakeHubApi()}

    def _factory(*args: Any, **kwargs: Any) -> _FakeHubApi:
        return state["api"]

    monkeypatch.setattr(orch._huggingface_hub, "HfApi", _factory)
    return state


# ---- Tests --------------------------------------------------------------------


def test_default_verifier_queries_repo_info(patched_hf) -> None:
    factory = default_hub_verifier_factory()
    api = patched_hf["api"]
    content_a = b"# README"
    content_b = b'{"x":1}'
    api._files = {"README.md": content_a, "stats.json": content_b}
    items = (
        UploadItem(
            relative_path="README.md",
            size_bytes=len(content_a),
            sha256=hashlib.sha256(content_a).hexdigest(),
        ),
        UploadItem(
            relative_path="stats.json",
            size_bytes=len(content_b),
            sha256=hashlib.sha256(content_b).hexdigest(),
        ),
    )
    assert factory(REPO_ID, items) == api._repo_sha


def test_default_verifier_uses_lfs_sha_when_present(patched_hf) -> None:
    """If Hub reports an LFS SHA-256, the verifier uses it for comparison."""
    factory = default_hub_verifier_factory()
    lfs_data = b"\x00" * (6 * 1024 * 1024)
    lfs_sha = hashlib.sha256(lfs_data).hexdigest()

    # Hub reports LFS metadata directly (without size verification).
    class _LfsApi(_FakeHubApi):
        def get_paths_info(self, repo_id: str, paths, *, revision: str, repo_type: str = "dataset"):  # type: ignore[override]
            out = []
            for path in paths:
                if path == "data/a.parquet":
                    out.append(_LfsPathInfo(path, len(lfs_data), lfs_sha))
            return out

        def get_hf_file_metadata(self, *args: Any, **kwargs: Any) -> Any:
            raise AssertionError("Should not be called when LFS sha present")

        def hf_hub_download(self, *args: Any, **kwargs: Any) -> Any:
            raise AssertionError("Should not be called when LFS sha present")

    api2 = _LfsApi()
    patched_hf["api"] = api2
    factory = default_hub_verifier_factory()
    items = (UploadItem(relative_path="data/a.parquet", size_bytes=len(lfs_data), sha256=lfs_sha),)
    assert factory(REPO_ID, items) == api2._repo_sha


def test_default_verifier_falls_back_to_hub_download_for_small_files(patched_hf) -> None:
    """For non-LFS files the verifier reads the file via Hub download and hashes it."""
    factory = default_hub_verifier_factory()
    content = b"# README\n"
    api = patched_hf["api"]
    api._files = {"README.md": content}
    items = (
        UploadItem(
            relative_path="README.md",
            size_bytes=len(content),
            sha256=hashlib.sha256(content).hexdigest(),
        ),
    )
    assert factory(REPO_ID, items) == api._repo_sha


def test_default_verifier_fails_on_missing_remote_file(patched_hf) -> None:
    factory = default_hub_verifier_factory()
    items = (
        UploadItem(relative_path="data/missing.parquet", size_bytes=10, sha256="x" * 64),
        UploadItem(relative_path="README.md", size_bytes=2, sha256="y" * 64),
    )
    api = patched_hf["api"]
    api._files = {"README.md": b"# R"}
    with pytest.raises(RuntimeError, match="missing"):
        factory(REPO_ID, items)


def test_default_verifier_fails_on_remote_hash_mismatch(patched_hf) -> None:
    factory = default_hub_verifier_factory()
    content = b"different content"
    items = (
        UploadItem(
            relative_path="data/a.parquet",
            size_bytes=len(content),
            sha256="0" * 64,
        ),
    )
    api = patched_hf["api"]
    api._files = {"data/a.parquet": content}
    with pytest.raises(RuntimeError, match="SHA mismatch"):
        factory(REPO_ID, items)


def test_default_verifier_raises_on_unauthenticated(patched_hf) -> None:
    factory = default_hub_verifier_factory()
    api = patched_hf["api"]
    api.fail_whoami(RuntimeError("not authenticated"))
    with pytest.raises(RuntimeError, match="authenticated"):
        factory(REPO_ID, ())


def test_default_verifier_returns_real_repo_sha_not_local_stub(patched_hf) -> None:
    factory = default_hub_verifier_factory()
    api = patched_hf["api"]
    api._repo_sha = "realhub-sha-aaaa"
    api._files = {"README.md": b"# "}
    items = (
        UploadItem(
            relative_path="README.md",
            size_bytes=2,
            sha256=hashlib.sha256(b"# ").hexdigest(),
        ),
    )
    assert factory(REPO_ID, items) == "realhub-sha-aaaa"


class _LfsPathInfo:
    def __init__(self, path: str, size: int, sha: str):
        self.path = path
        self.size = size
        self.lfs = type("_L", (), {"sha256": sha})()
        self.sha256 = sha
