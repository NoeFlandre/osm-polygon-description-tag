"""Remote Hugging Face identity verification."""

from __future__ import annotations

from pathlib import Path
from typing import Any, Protocol

from osm_polygon_description_tag.dataset.manifest import file_sha256
from osm_polygon_description_tag.publication.hub_client import (
    _huggingface_hub,
    _HuggingFaceHub,
    new_hf_api,
)
from osm_polygon_description_tag.publication.models import UploadItem


class HubVerifier(Protocol):
    """Verify published files and the pinned published inventory."""

    def __call__(self, repo_id: str, files: tuple[UploadItem, ...]) -> str: ...

    def matching_revision(
        self,
        repo_id: str,
        files: tuple[UploadItem, ...],
    ) -> str | None: ...

    def verify_inventory(
        self,
        repo_id: str,
        files: tuple[UploadItem, ...],
        *,
        revision: str | None = None,
    ) -> str: ...


class HubVerificationError(RuntimeError):
    """Raised when the default Hub verifier cannot confirm a remote fact."""


class _RemoteFileMismatch(HubVerificationError):
    """Expected content mismatch used by the idempotence probe."""


def _entries_by_path(requested_paths: list[str], entries: list[Any]) -> dict[str, Any]:
    """Associate one batched Hub response with requested paths.

    Older test doubles and some compatible Hub clients omit ``path`` on the
    response objects, so their documented response order is retained as a
    fallback. Real Hub entries carry paths and are keyed explicitly.
    """
    if all(isinstance(getattr(entry, "path", None), str) for entry in entries):
        return {str(entry.path): entry for entry in entries}
    # Non-strict on purpose: a short response *is* the missing-file case, and
    # the caller reports it against the exact path. ``strict=False`` is also
    # the default, so writing it either way cannot change behaviour.
    pairs = zip(requested_paths, entries, strict=False)  # pragma: no mutate
    return {path: entry for path, entry in pairs}


def _authenticated_api() -> Any:
    # Resolve the HfApi lazily at invocation time so monkeypatching
    # _huggingface_hub.HfApi is honored by tests.
    api = new_hf_api()
    try:
        identity = api.whoami()
    except Exception as error:
        raise HubVerificationError(f"Hub authentication failed: {error}") from error
    if not identity:
        raise HubVerificationError("Hub authentication returned no identity")
    return api


def _repository_revision(api: Any, repo_id: str) -> str:
    try:
        info = api.repo_info(repo_id, repo_type="dataset")
    except Exception as error:
        raise HubVerificationError(
            f"Hub repository {repo_id} is not accessible: {error}"
        ) from error
    # pragma: no mutate start - missing SHA defaults are normalized below
    repo_sha = getattr(info, "sha", None)
    # pragma: no mutate end
    revision = str(repo_sha or "")
    if not revision:
        raise HubVerificationError(f"Hub repository {repo_id} returned an empty revision")
    return revision


def _paths_info(api: Any, repo_id: str, requested_paths: list[str], revision: str) -> list[Any]:
    try:
        return list(
            api.get_paths_info(
                repo_id,
                paths=requested_paths,
                revision=revision,
                repo_type="dataset",
            )
        )
    except Exception as error:
        raise HubVerificationError(
            f"hub verification failed for {', '.join(requested_paths)}: {error}"
        ) from error


def _check_entry_metadata(item: UploadItem, entry: Any, revision: str) -> bool:
    """Check size and LFS identity; return whether the LFS SHA settled the file."""
    if entry is None:
        raise _RemoteFileMismatch(
            f"remote file missing in revision {revision}: {item.relative_path}"
        )
    _check_entry_size(item, entry)
    return _lfs_sha_settles(item, entry)


def _check_entry_size(item: UploadItem, entry: Any) -> None:
    size = getattr(entry, "size", None)
    if size is not None and int(size) != int(item.size_bytes):
        raise _RemoteFileMismatch(
            f"remote size mismatch for {item.relative_path}: local={item.size_bytes}, remote={size}"
        )


def _lfs_sha_settles(item: UploadItem, entry: Any) -> bool:
    lfs_info = getattr(entry, "lfs", None)
    lfs_sha = getattr(lfs_info, "sha256", None) if lfs_info is not None else None
    if not lfs_sha:
        return False
    if str(lfs_sha).lower() != str(item.sha256).lower():
        raise _RemoteFileMismatch(f"remote LFS SHA mismatch for {item.relative_path}")
    return True


def _download_for_hash(
    api: Any, repo_id: str, item: UploadItem, revision: str, cache_dir: Path | None
) -> Any:
    # Fallback: read the remote content via hf_hub_download for direct
    # SHA-256 comparison. This is the authoritative identity for small
    # non-LFS files.
    try:
        download_kwargs: dict[str, object] = {
            "revision": revision,
            "repo_type": "dataset",
        }
        if cache_dir is not None:
            download_kwargs["cache_dir"] = cache_dir
        return api.hf_hub_download(
            repo_id,
            item.relative_path,
            **download_kwargs,
        )
    except Exception as error:
        raise HubVerificationError(
            f"could not download {item.relative_path} from {repo_id}@{revision}: {error}"
        ) from error


def _verify_downloaded_content(
    api: Any, repo_id: str, item: UploadItem, revision: str, cache_dir: Path | None
) -> None:
    local_path = _download_for_hash(api, repo_id, item, revision, cache_dir)
    digest = file_sha256(Path(local_path))
    if digest.lower() != str(item.sha256).lower():
        raise _RemoteFileMismatch(
            f"remote SHA mismatch for {item.relative_path}: local={item.sha256}, remote={digest}"
        )


def _verify_files_at_revision(
    api: Any,
    repo_id: str,
    files: tuple[UploadItem, ...],
    revision: str,
    cache_dir: Path | None,
) -> None:
    requested_paths = [item.relative_path for item in files]
    if not requested_paths:
        return
    entries = _paths_info(api, repo_id, requested_paths, revision)
    entries_by_path = _entries_by_path(requested_paths, entries)
    for item in files:
        entry = entries_by_path.get(item.relative_path)
        if _check_entry_metadata(item, entry, revision):
            continue
        _verify_downloaded_content(api, repo_id, item, revision, cache_dir)


def _verify_current_revision(
    repo_id: str, files: tuple[UploadItem, ...], cache_dir: Path | None
) -> str:
    api = _authenticated_api()
    revision = _repository_revision(api, repo_id)
    _verify_files_at_revision(api, repo_id, files, revision, cache_dir)
    return revision


def _matching_revision(
    repo_id: str, files: tuple[UploadItem, ...], cache_dir: Path | None
) -> str | None:
    """Return the current revision when every intended file already matches.

    Authentication, repository lookup, and operational verification
    failures propagate as :class:`HubVerificationError`. A missing file or
    content mismatch is an expected non-match and returns ``None``.
    """
    api = _authenticated_api()
    revision = _repository_revision(api, repo_id)
    try:
        _verify_files_at_revision(api, repo_id, files, revision, cache_dir)
    except _RemoteFileMismatch:
        return None
    except HubVerificationError as error:
        raise HubVerificationError(
            f"remote metadata verification failed for {repo_id}@{revision}: {error}"
        ) from error
    return revision


def _read_file(repo_id: str, path: str, revision: str, cache_dir: Path | None) -> str:
    """Read one small text artifact from an exact Hub revision."""
    api = _authenticated_api()
    try:
        local_path = api.hf_hub_download(
            repo_id,
            path,
            revision=revision,
            repo_type="dataset",
            **({"cache_dir": cache_dir} if cache_dir is not None else {}),
        )
        text = Path(local_path).read_bytes().decode()
        return text.replace("\r\n", "\n").replace("\r", "\n")
    except Exception as error:
        raise HubVerificationError(
            f"could not read {path} from {repo_id}@{revision}: {error}"
        ) from error


def _remote_inventory_paths(api: Any, repo_id: str, revision: str) -> set[str]:
    try:
        return set(
            api.list_repo_files(
                repo_id,
                revision=revision,
                repo_type="dataset",
            )
        )
    except Exception as error:
        raise HubVerificationError(
            f"hub inventory lookup failed at revision {revision}: {error}"
        ) from error


def _managed_paths(paths: set[str]) -> set[str]:
    return {path for path in paths if path.startswith("data/") or path.startswith("manifests/")}


def _verify_inventory(
    repo_id: str,
    files: tuple[UploadItem, ...],
    revision: str | None,
    cache_dir: Path | None,
) -> str:
    api = _authenticated_api()
    resolved_revision = revision or _repository_revision(api, repo_id)
    remote_inventory = _managed_paths(_remote_inventory_paths(api, repo_id, resolved_revision))
    expected_inventory = {item.relative_path for item in files}
    if remote_inventory != expected_inventory:
        raise HubVerificationError(
            f"remote data/manifest inventory path mismatch at revision "
            f"{resolved_revision}: expected={sorted(expected_inventory)}, "
            f"remote={sorted(remote_inventory)}"
        )
    _verify_files_at_revision(api, repo_id, files, resolved_revision, cache_dir)
    return resolved_revision


def _reconcile_managed_files(repo_id: str, expected_paths: set[str]) -> str | None:
    """Delete only stale files in the dataset's managed artifact namespaces."""
    api = new_hf_api()
    if not hasattr(api, "list_repo_files") or not hasattr(api, "delete_files"):
        return None
    remote_paths = set(api.list_repo_files(repo_id, repo_type="dataset"))
    stale = sorted(_managed_paths(remote_paths - expected_paths))
    if not stale:
        return None
    commit = api.delete_files(
        repo_id,
        stale,
        repo_type="dataset",
        commit_message="Remove stale generated dataset artifacts",
    )
    return _deletion_revision(api, repo_id, commit)


def _deletion_revision(api: Any, repo_id: str, commit: Any) -> str | None:
    revision = getattr(commit, "oid", None)
    if revision:
        return str(revision)
    info = api.repo_info(repo_id, repo_type="dataset")
    # pragma: no mutate start - missing SHA defaults are normalized below
    repo_sha = getattr(info, "sha", None)
    # pragma: no mutate end
    return str(repo_sha or "") or None


class _DefaultHubVerifier:
    """The live-Hub :class:`HubVerifier`, bound to one optional cache directory."""

    __slots__ = ("_cache_dir",)

    def __init__(self, cache_dir: Path | None) -> None:
        self._cache_dir = cache_dir

    def __call__(self, repo_id: str, files: tuple[UploadItem, ...]) -> str:
        return _verify_current_revision(repo_id, files, self._cache_dir)

    def matching_revision(
        self,
        repo_id: str,
        files: tuple[UploadItem, ...],
    ) -> str | None:
        return _matching_revision(repo_id, files, self._cache_dir)

    def verify_inventory(
        self,
        repo_id: str,
        files: tuple[UploadItem, ...],
        *,
        revision: str | None = None,
    ) -> str:
        return _verify_inventory(repo_id, files, revision, self._cache_dir)

    def read_file(self, repo_id: str, path: str, *, revision: str) -> str:
        return _read_file(repo_id, path, revision, self._cache_dir)

    def reconcile_managed_files(self, repo_id: str, expected_paths: set[str]) -> str | None:
        return _reconcile_managed_files(repo_id, expected_paths)


def default_hub_verifier_factory(*, cache_dir: Path | None = None) -> HubVerifier:
    """Return a verifier that talks to the live Hugging Face Hub.

    The verifier:

    1. Confirms the caller's authenticated identity via ``HfApi.whoami``.
    2. For an inventory verification, lists the complete remote ``data/`` and
       ``manifests/`` namespaces at a pinned revision and rejects any path
       mismatch before metadata publication.
    3. For each :class:`UploadItem` it checks ``HfApi.get_paths_info`` for the
       exact file metadata at that revision; small files are read via
       ``HfApi.hf_hub_download`` and hashed with SHA-256, larger files are
       compared against the LFS ``sha256`` reported in the Hub metadata.
    4. Queries the dataset repository and reads its current commit SHA via
       ``HfApi.repo_info`` when no revision is supplied.
    5. Returns the verified commit SHA, or raises :class:`HubVerificationError`
       on a mismatch / missing file / unauthenticated identity. The dedicated
       ``matching_revision`` probe returns ``None`` for an expected content
       mismatch so a caller can safely decide to publish.

    The ``HfApi`` is resolved at invocation time (not at factory time), so
    tests may monkeypatch ``orch._huggingface_hub.HfApi`` BEFORE the
    verifier is actually called.
    """
    return _DefaultHubVerifier(cache_dir)


def build_default_hub_verifier() -> HubVerifier:
    """Build a fresh default Hub verifier."""
    return default_hub_verifier_factory()


__all__ = [
    "HubVerificationError",
    "HubVerifier",
    "_HuggingFaceHub",
    "_huggingface_hub",
    "build_default_hub_verifier",
    "default_hub_verifier_factory",
]
