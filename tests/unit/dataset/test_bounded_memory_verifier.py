"""RED tests proving the Hub verifier uses bounded-memory streaming hashing.

The current verifier re-reads the downloaded remote file with
``hashlib.sha256(Path(local_path).read_bytes())``. That loads the entire
file into memory and can OOM the process for large datasets.

The amendment:

- The hash must be computed incrementally using ``file_sha256`` (or an
  equivalent bounded streaming helper) from
  ``osm_polygon_description_tag.dataset.manifest``.
- A regression test makes ``Path.read_bytes()`` raise and proves that
  fallback remote verification still succeeds through streaming hashing.
"""

from __future__ import annotations

import hashlib
import os
from pathlib import Path
from typing import Any

import pytest

from osm_polygon_description_tag.orchestrator import default_hub_verifier_factory
from osm_polygon_description_tag.publication import REPO_ID, UploadItem


class _FakeHubApi:
    """In-process HfApi replacement that supports read_bytes() simulation."""

    def __init__(self, *, repo_sha: str, files: dict[str, bytes]):
        self._repo_sha = repo_sha
        self._files = files
        self.calls: list[tuple[str, tuple]] = []

    def whoami(self) -> object:
        self.calls.append(("whoami", ()))
        return {"name": "fake"}

    def repo_info(self, *_a: object, **_kw: object) -> object:
        self.calls.append(("repo_info", (_a, _kw)))
        info = _Info(self._repo_sha)
        return info

    def get_paths_info(
        self,
        repo_id: str,
        paths: list[str],
        *,
        revision: str,
        repo_type: str = "dataset",
    ) -> list[Any]:
        self.calls.append(
            (
                "get_paths_info",
                (repo_id, list(paths)),
                {"revision": revision, "repo_type": repo_type},
            )
        )
        out = []
        for rel in paths:
            content = self._files.get(rel)
            if content is None:
                continue
            out.append(
                _PathInfo(path=rel, size=len(content), sha=hashlib.sha256(content).hexdigest())
            )
        return out

    def hf_hub_download(
        self,
        repo_id: str,
        filename: str,
        *,
        revision: str,
        repo_type: str = "dataset",
    ) -> str:
        self.calls.append(("hf_hub_download", (repo_id, filename, revision, repo_type)))
        content = self._files[filename]
        # Write the content to a temp file using the file-style interface
        # so the verifier can stream-hash it without read_bytes().
        import tempfile

        fd, tmp_path = tempfile.mkstemp(prefix="hf-download-")
        try:
            os.write(fd, content)
        finally:
            os.close(fd)
        return tmp_path


class _Info:
    def __init__(self, sha: str) -> None:
        self.sha = sha


class _PathInfo:
    def __init__(self, *, path: str, size: int, sha: str) -> None:
        self.path = path
        self.size = size
        self.sha = sha
        self.lfs = None


def _patch_hub(monkeypatch: pytest.MonkeyPatch, hub: _FakeHubApi) -> None:
    import osm_polygon_description_tag.orchestrator as orch

    def factory(*a: object, **kw: object) -> _FakeHubApi:
        return hub

    monkeypatch.setattr(orch._huggingface_hub, "HfApi", factory)


def test_verifier_uses_streaming_hash_when_read_bytes_raises(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Fallback verification must succeed even if ``Path.read_bytes()`` raises."""
    real_content = b"# README\n" * 1000
    expected_sha = hashlib.sha256(real_content).hexdigest()
    items = (
        UploadItem(
            relative_path="README.md",
            size_bytes=len(real_content),
            sha256=expected_sha,
        ),
    )
    hub = _FakeHubApi(repo_sha="rev-1", files={"README.md": real_content})
    _patch_hub(monkeypatch, hub)

    # Make ``Path.read_bytes`` raise to prove the verifier cannot rely on it.
    def read_bytes_forbidden(self: Path) -> bytes:
        raise AssertionError("verifier must not call Path.read_bytes(); use streaming hash")

    monkeypatch.setattr(Path, "read_bytes", read_bytes_forbidden)

    factory = default_hub_verifier_factory()
    assert factory(REPO_ID, items) == "rev-1"

    # The verifier must have actually downloaded the file to verify content.
    hf_downloads = [c for c in hub.calls if c[0] == "hf_hub_download"]
    assert hf_downloads, "verifier did not call hf_hub_download"


def test_verifier_does_not_call_read_bytes_for_large_file(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """The verifier must not call ``Path.read_bytes()`` even for large files."""
    # 1 MB of pseudo-random content.
    real_content = os.urandom(1024 * 1024)
    expected_sha = hashlib.sha256(real_content).hexdigest()
    items = (
        UploadItem(
            relative_path="data/big.parquet",
            size_bytes=len(real_content),
            sha256=expected_sha,
        ),
    )
    hub = _FakeHubApi(repo_sha="rev-big", files={"data/big.parquet": real_content})
    _patch_hub(monkeypatch, hub)

    def read_bytes_forbidden(self: Path) -> bytes:
        raise AssertionError("verifier must avoid Path.read_bytes()")

    monkeypatch.setattr(Path, "read_bytes", read_bytes_forbidden)

    factory = default_hub_verifier_factory()
    assert factory(REPO_ID, items) == "rev-big"
