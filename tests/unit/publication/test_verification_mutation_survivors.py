"""Focused regression tests for publication verification mutation survivors."""

from __future__ import annotations

import hashlib
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from osm_polygon_description_tag.publication import verification
from osm_polygon_description_tag.publication.models import REPO_ID, UploadItem


def _item(path: str, content: bytes) -> UploadItem:
    return UploadItem(
        relative_path=path,
        size_bytes=len(content),
        sha256=hashlib.sha256(content).hexdigest(),
    )


class _MatchingFallbackApi:
    def __init__(self, tmp_path: Path, content: bytes, cache_dir: Path) -> None:
        self.tmp_path = tmp_path
        self.content = content
        self.cache_dir = cache_dir
        self.download_calls: list[tuple[Any, ...]] = []

    def whoami(self) -> dict[str, str]:
        return {"name": "user"}

    def repo_info(self, repo_id: str, *, repo_type: str) -> SimpleNamespace:
        assert repo_id == REPO_ID
        assert repo_type == "dataset"
        return SimpleNamespace(sha="pinned-revision")

    def get_paths_info(
        self,
        repo_id: str,
        *,
        paths: list[str],
        revision: str,
        repo_type: str,
    ) -> list[SimpleNamespace]:
        assert repo_id == REPO_ID
        assert revision == "pinned-revision"
        assert repo_type == "dataset"
        return [
            SimpleNamespace(path=path, size=len(self.content), lfs=SimpleNamespace())
            for path in paths
        ]

    def hf_hub_download(
        self,
        repo_id: str,
        filename: str,
        *,
        revision: str,
        repo_type: str,
        cache_dir: Path | None = None,
    ) -> str:
        self.download_calls.append((repo_id, filename, revision, repo_type, cache_dir))
        assert cache_dir == self.cache_dir
        downloaded = self.tmp_path / "downloaded" / filename
        downloaded.parent.mkdir(parents=True, exist_ok=True)
        downloaded.write_bytes(self.content)
        return str(downloaded)


def test_matching_revision_forwards_cache_dir_to_download_fallback(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    content = b"remote card without LFS metadata"
    item = _item("README.md", content)
    cache_dir = tmp_path / "hub-cache"
    api = _MatchingFallbackApi(tmp_path, content, cache_dir)
    monkeypatch.setattr(verification._huggingface_hub, "HfApi", lambda: api)

    verifier = verification.default_hub_verifier_factory(cache_dir=cache_dir)

    assert verifier.matching_revision(REPO_ID, (item,)) == "pinned-revision"
    assert api.download_calls == [
        (REPO_ID, item.relative_path, "pinned-revision", "dataset", cache_dir)
    ]


def test_missing_file_mismatch_names_the_revision_being_verified() -> None:
    item = _item("data/missing.parquet", b"expected")

    class _Api:
        def get_paths_info(
            self,
            repo_id: str,
            *,
            paths: list[str],
            revision: str,
            repo_type: str,
        ) -> list[object]:
            assert repo_id == REPO_ID
            assert paths == [item.relative_path]
            assert revision == "pinned-revision"
            assert repo_type == "dataset"
            return []

    with pytest.raises(verification.HubVerificationError) as raised:
        verification._verify_files_at_revision(_Api(), REPO_ID, (item,), "pinned-revision", None)

    assert str(raised.value) == (
        f"remote file missing in revision pinned-revision: {item.relative_path}"
    )


def test_read_file_decodes_utf8_and_normalizes_universal_newlines(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    local = tmp_path / "README.md"
    local.write_bytes("café\r\nline two\rline three\n".encode())

    class _Api:
        def hf_hub_download(self, *args: object, **kwargs: object) -> str:
            assert args == (REPO_ID, "README.md")
            assert kwargs == {"revision": "pinned-revision", "repo_type": "dataset"}
            return str(local)

    monkeypatch.setattr(verification, "_authenticated_api", lambda: _Api())
    assert verification._read_file(REPO_ID, "README.md", "pinned-revision", None) == (
        "café\nline two\nline three\n"
    )
