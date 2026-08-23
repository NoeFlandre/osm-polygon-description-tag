"""Focused behavioral coverage for the default Hub verifier."""

from __future__ import annotations

import hashlib
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

import osm_polygon_description_tag.publication.verification as verification
from osm_polygon_description_tag.publication.models import REPO_ID, UploadItem


def _item(path: str, content: bytes) -> UploadItem:
    return UploadItem(
        relative_path=path,
        size_bytes=len(content),
        sha256=hashlib.sha256(content).hexdigest(),
    )


class _StrictEntry:
    def __init__(
        self,
        *,
        size: int | None = None,
        lfs_sha: str | None = None,
        include_size: bool = True,
        include_lfs_sha: bool = True,
    ) -> None:
        if include_size:
            self.size = size
        if include_lfs_sha:
            self.lfs = SimpleNamespace(sha256=lfs_sha) if lfs_sha is not None else SimpleNamespace()


class _StrictVerifierHub:
    def __init__(
        self, tmp_path: Path, entries: dict[str, _StrictEntry], contents: dict[str, bytes]
    ):
        self.tmp_path = tmp_path
        self.entries = entries
        self.contents = contents
        self.calls: list[tuple[str, Any, Any]] = []

    def whoami(self) -> dict[str, str]:
        self.calls.append(("whoami", (), {}))
        return {"name": "user"}

    def repo_info(self, repo_id: str, *, repo_type: str) -> SimpleNamespace:
        assert repo_id == REPO_ID
        assert repo_type == "dataset"
        self.calls.append(("repo_info", repo_id, repo_type))
        return SimpleNamespace(sha="revision-1")

    def get_paths_info(
        self,
        repo_id: str,
        *,
        paths: list[str],
        revision: str,
        repo_type: str,
    ) -> list[_StrictEntry]:
        assert repo_id == REPO_ID
        assert revision == "revision-1"
        assert repo_type == "dataset"
        self.calls.append(("get_paths_info", tuple(paths), revision))
        return [self.entries[path] for path in paths]

    def hf_hub_download(
        self,
        repo_id: str,
        filename: str,
        *,
        revision: str,
        repo_type: str,
        cache_dir: Path | None = None,
    ) -> str:
        assert repo_id == REPO_ID
        assert revision == "revision-1"
        assert repo_type == "dataset"
        assert cache_dir is not None
        self.calls.append(("hf_hub_download", filename, cache_dir))
        path = self.tmp_path / "downloaded" / filename
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(self.contents[filename])
        return str(path)


def _install_hub(monkeypatch: pytest.MonkeyPatch, hub: object) -> None:
    monkeypatch.setattr(verification._huggingface_hub, "HfApi", lambda: hub)


def test_default_verifier_checks_multiple_files_with_lfs_and_download_fallback(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    lfs_content = b"lfs"
    downloaded_content = b"downloaded content"
    lfs_item = _item("data/lfs.parquet", lfs_content)
    downloaded_item = _item("README.md", downloaded_content)
    hub = _StrictVerifierHub(
        tmp_path,
        {
            lfs_item.relative_path: _StrictEntry(
                size=lfs_item.size_bytes,
                lfs_sha=lfs_item.sha256.upper(),
            ),
            downloaded_item.relative_path: _StrictEntry(
                include_size=False,
                include_lfs_sha=False,
            ),
        },
        {downloaded_item.relative_path: downloaded_content},
    )
    _install_hub(monkeypatch, hub)

    verifier = verification.default_hub_verifier_factory(cache_dir=tmp_path / "cache")

    assert verifier(REPO_ID, (lfs_item, downloaded_item)) == "revision-1"
    assert hub.calls == [
        ("whoami", (), {}),
        ("repo_info", REPO_ID, "dataset"),
        ("get_paths_info", (lfs_item.relative_path,), "revision-1"),
        ("get_paths_info", (downloaded_item.relative_path,), "revision-1"),
        ("hf_hub_download", downloaded_item.relative_path, tmp_path / "cache"),
    ]


def test_download_fallback_uses_canonical_file_sha256(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    content = b"downloaded content"
    item = _item("README.md", content)
    hub = _StrictVerifierHub(
        tmp_path,
        {item.relative_path: _StrictEntry(include_size=False, include_lfs_sha=False)},
        {item.relative_path: content},
    )
    _install_hub(monkeypatch, hub)
    hashed_paths: list[Path] = []

    def fake_file_sha256(path: Path) -> str:
        hashed_paths.append(path)
        return item.sha256

    monkeypatch.setattr(verification, "file_sha256", fake_file_sha256, raising=False)
    verifier = verification.default_hub_verifier_factory(cache_dir=tmp_path / "cache")

    assert verifier(REPO_ID, (item,)) == "revision-1"
    assert hashed_paths == [tmp_path / "downloaded" / item.relative_path]


def test_default_verifier_rejects_empty_identity_with_exact_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class EmptyIdentity:
        def whoami(self) -> None:
            return None

    _install_hub(monkeypatch, EmptyIdentity())
    verifier = verification.default_hub_verifier_factory()

    with pytest.raises(
        verification.HubVerificationError,
        match=r"^Hub authentication returned no identity$",
    ):
        verifier(REPO_ID, ())


def test_default_verifier_rejects_missing_revision(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class MissingRevision:
        def whoami(self) -> dict[str, str]:
            return {"name": "user"}

        def repo_info(self, _repo_id: str, *, repo_type: str) -> SimpleNamespace:
            assert repo_type == "dataset"
            return SimpleNamespace()

    _install_hub(monkeypatch, MissingRevision())
    verifier = verification.default_hub_verifier_factory()

    with pytest.raises(verification.HubVerificationError, match="empty revision"):
        verifier(REPO_ID, ())


def test_default_verifier_translates_repository_lookup_errors(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class FailingRepoInfo:
        def whoami(self) -> dict[str, str]:
            return {"name": "user"}

        def repo_info(self, _repo_id: str, *, repo_type: str) -> SimpleNamespace:
            assert repo_type == "dataset"
            raise RuntimeError("repository unavailable")

    _install_hub(monkeypatch, FailingRepoInfo())
    verifier = verification.default_hub_verifier_factory()

    with pytest.raises(
        verification.HubVerificationError,
        match=rf"^Hub repository {REPO_ID} is not accessible: repository unavailable$",
    ):
        verifier(REPO_ID, ())


def test_default_verifier_translates_path_lookup_errors(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    item = _item("README.md", b"readme")

    class FailingPaths(_StrictVerifierHub):
        def get_paths_info(self, _repo_id: str, **_kwargs: Any) -> list[_StrictEntry]:
            raise RuntimeError("metadata unavailable")

    hub = FailingPaths(tmp_path, {}, {})
    _install_hub(monkeypatch, hub)
    verifier = verification.default_hub_verifier_factory()

    with pytest.raises(
        verification.HubVerificationError,
        match=rf"^hub verification failed for {item.relative_path}: metadata unavailable$",
    ):
        verifier(REPO_ID, (item,))


def test_default_verifier_translates_download_errors(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    item = _item("README.md", b"readme")

    class FailingDownload(_StrictVerifierHub):
        def hf_hub_download(
            self,
            _repo_id: str,
            _filename: str,
            **_kwargs: Any,
        ) -> str:
            raise RuntimeError("download unavailable")

    hub = FailingDownload(
        tmp_path,
        {item.relative_path: _StrictEntry(include_size=False, include_lfs_sha=False)},
        {},
    )
    _install_hub(monkeypatch, hub)
    verifier = verification.default_hub_verifier_factory()

    with pytest.raises(
        verification.HubVerificationError,
        match=rf"^could not download {item.relative_path} from {REPO_ID}@revision-1: "
        r"download unavailable$",
    ):
        verifier(REPO_ID, (item,))


def test_default_verifier_rejects_size_and_lfs_sha_mismatches(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    content = b"content"
    item = _item("data/a.parquet", content)
    size_hub = _StrictVerifierHub(
        tmp_path,
        {item.relative_path: _StrictEntry(size=item.size_bytes + 1, lfs_sha=item.sha256)},
        {},
    )
    _install_hub(monkeypatch, size_hub)
    verifier = verification.default_hub_verifier_factory()
    with pytest.raises(verification.HubVerificationError, match="size mismatch"):
        verifier(REPO_ID, (item,))

    lfs_hub = _StrictVerifierHub(
        tmp_path,
        {item.relative_path: _StrictEntry(size=item.size_bytes, lfs_sha="0" * 64)},
        {},
    )
    _install_hub(monkeypatch, lfs_hub)
    with pytest.raises(verification.HubVerificationError, match="LFS SHA mismatch"):
        verifier(REPO_ID, (item,))


def test_default_verifier_rejects_missing_remote_file(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    item = _item("README.md", b"readme")

    class MissingFile(_StrictVerifierHub):
        def get_paths_info(self, _repo_id: str, **_kwargs: Any) -> list[_StrictEntry]:
            return []

    hub = MissingFile(tmp_path, {}, {})
    _install_hub(monkeypatch, hub)
    verifier = verification.default_hub_verifier_factory()

    with pytest.raises(verification.HubVerificationError, match="remote file missing"):
        verifier(REPO_ID, (item,))


def test_default_verifier_rejects_download_hash_mismatch(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    item = _item("README.md", b"expected")
    hub = _StrictVerifierHub(
        tmp_path,
        {item.relative_path: _StrictEntry(include_size=False, include_lfs_sha=False)},
        {item.relative_path: b"different"},
    )
    _install_hub(monkeypatch, hub)
    verifier = verification.default_hub_verifier_factory(cache_dir=tmp_path / "cache")

    with pytest.raises(verification.HubVerificationError, match="remote SHA mismatch"):
        verifier(REPO_ID, (item,))


def test_default_verifier_downloads_when_lfs_metadata_has_no_sha256(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    content = b"content without an LFS digest"
    item = _item("data/small.parquet", content)
    hub = _StrictVerifierHub(
        tmp_path,
        {
            item.relative_path: _StrictEntry(
                size=item.size_bytes,
                include_lfs_sha=True,
            )
        },
        {item.relative_path: content},
    )
    _install_hub(monkeypatch, hub)
    verifier = verification.default_hub_verifier_factory(cache_dir=tmp_path / "cache")

    assert verifier(REPO_ID, (item,)) == "revision-1"
    assert hub.calls[-1] == ("hf_hub_download", item.relative_path, tmp_path / "cache")


def test_reconcile_managed_files_deletes_only_sorted_stale_names(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class Hub:
        def __init__(self) -> None:
            self.deleted: tuple[str, list[str], str, str] | None = None

        def list_repo_files(self, repo_id: str, *, repo_type: str) -> list[str]:
            assert repo_id == REPO_ID
            assert repo_type == "dataset"
            return [
                "README.md",
                "data/z.parquet",
                "data/a.parquet",
                "manifests/old.json",
                "other/stale.txt",
            ]

        def delete_files(
            self,
            repo_id: str,
            paths: list[str],
            *,
            repo_type: str,
            commit_message: str,
        ) -> SimpleNamespace:
            assert repo_id == REPO_ID
            assert repo_type == "dataset"
            self.deleted = (repo_id, paths, repo_type, commit_message)
            return SimpleNamespace(oid="delete-revision")

    hub = Hub()
    _install_hub(monkeypatch, hub)
    verifier = verification.default_hub_verifier_factory()

    revision = verifier.reconcile_managed_files(
        REPO_ID,
        {"data/a.parquet", "manifests/keep.json"},
    )

    assert revision == "delete-revision"
    assert hub.deleted == (
        REPO_ID,
        ["data/z.parquet", "manifests/old.json"],
        "dataset",
        "Remove stale generated dataset artifacts",
    )


def test_reconcile_managed_files_skips_when_no_stale_files(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class Hub:
        def list_repo_files(self, repo_id: str, *, repo_type: str) -> list[str]:
            assert repo_id == REPO_ID
            assert repo_type == "dataset"
            return ["data/a.parquet"]

        def delete_files(self, *_args: Any, **_kwargs: Any) -> None:
            raise AssertionError("delete_files must not run without stale files")

    _install_hub(monkeypatch, Hub())
    verifier = verification.default_hub_verifier_factory()
    assert verifier.reconcile_managed_files(REPO_ID, {"data/a.parquet"}) is None


@pytest.mark.parametrize("missing", ["list", "delete"])
def test_reconcile_managed_files_requires_both_hub_capabilities(
    monkeypatch: pytest.MonkeyPatch,
    missing: str,
) -> None:
    class OnlyList:
        def list_repo_files(self, _repo_id: str, *, repo_type: str) -> list[str]:
            assert repo_type == "dataset"
            return ["data/stale.parquet"]

    class OnlyDelete:
        def delete_files(self, *_args: Any, **_kwargs: Any) -> None:
            raise AssertionError("delete_files must not run")

    hub = OnlyDelete() if missing == "list" else OnlyList()
    _install_hub(monkeypatch, hub)
    verifier = verification.default_hub_verifier_factory()

    assert verifier.reconcile_managed_files(REPO_ID, set()) is None


def test_reconcile_managed_files_falls_back_to_repo_sha_when_commit_has_no_oid(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class Hub:
        def list_repo_files(self, _repo_id: str, *, repo_type: str) -> list[str]:
            assert repo_type == "dataset"
            return ["data/stale.parquet"]

        def delete_files(
            self,
            _repo_id: str,
            _paths: list[str],
            *,
            repo_type: str,
            commit_message: str,
        ) -> SimpleNamespace:
            assert repo_type == "dataset"
            assert commit_message == "Remove stale generated dataset artifacts"
            return SimpleNamespace()

        def repo_info(self, repo_id: str, *, repo_type: str) -> SimpleNamespace:
            assert repo_id == REPO_ID
            assert repo_type == "dataset"
            return SimpleNamespace(sha="fallback-revision")

    _install_hub(monkeypatch, Hub())
    verifier = verification.default_hub_verifier_factory()

    assert verifier.reconcile_managed_files(REPO_ID, set()) == "fallback-revision"


def test_reconcile_managed_files_returns_none_when_fallback_sha_is_missing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class Hub:
        def list_repo_files(self, _repo_id: str, *, repo_type: str) -> list[str]:
            assert repo_type == "dataset"
            return ["data/stale.parquet"]

        def delete_files(
            self,
            _repo_id: str,
            _paths: list[str],
            *,
            repo_type: str,
            commit_message: str,
        ) -> SimpleNamespace:
            assert repo_type == "dataset"
            assert commit_message == "Remove stale generated dataset artifacts"
            return SimpleNamespace()

        def repo_info(self, _repo_id: str, *, repo_type: str) -> SimpleNamespace:
            assert repo_type == "dataset"
            return SimpleNamespace()

    _install_hub(monkeypatch, Hub())
    verifier = verification.default_hub_verifier_factory()

    assert verifier.reconcile_managed_files(REPO_ID, set()) is None


def test_verifier_reconcile_method_forwards_arguments(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class Hub:
        def list_repo_files(self, repo_id: str, *, repo_type: str) -> list[str]:
            assert repo_id == REPO_ID
            assert repo_type == "dataset"
            return []

    _install_hub(monkeypatch, Hub())
    verifier = verification.default_hub_verifier_factory()
    assert verifier.reconcile_managed_files(REPO_ID, {"data/a.parquet"}) is None


def test_lazy_hub_wrapper_resolves_once_and_forwards_attribute(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    module = SimpleNamespace(answer=42)
    monkeypatch.setitem(__import__("sys").modules, "huggingface_hub", module)
    hub = verification._HuggingFaceHub()

    assert hub._module is None
    assert hub._resolve_module() is module
    assert hub._resolve_module() is module
    assert hub.answer == 42


def test_lazy_hub_wrapper_uses_existing_module_without_import() -> None:
    module = SimpleNamespace(answer=42)
    hub = verification._HuggingFaceHub()
    hub._module = module

    assert hub.answer == 42
