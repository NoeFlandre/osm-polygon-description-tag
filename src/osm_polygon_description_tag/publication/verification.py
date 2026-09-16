"""Remote Hugging Face identity verification."""

from __future__ import annotations

from pathlib import Path
from typing import Any, Protocol

from osm_polygon_description_tag.dataset.manifest import file_sha256
from osm_polygon_description_tag.publication.models import UploadItem


class HubVerifier(Protocol):
    """Verify uploaded files and the pinned published inventory."""

    def __call__(self, repo_id: str, files: tuple[UploadItem, ...]) -> str: ...

    def verify_inventory(
        self,
        repo_id: str,
        files: tuple[UploadItem, ...],
        *,
        revision: str | None = None,
    ) -> str: ...


class _HuggingFaceHub:
    """Lazy wrapper around the huggingface_hub package.

    Importing huggingface_hub at module load time would couple the project
    to a network-authenticated dependency even for read-only operations.
    The wrapper defers the import until :func:`default_hub_verifier_factory`
    actually instantiates a verifier.

    Tests may override attributes on this instance (for example,
    ``_huggingface_hub.HfApi = lambda ...``); those overrides take
    precedence over the lazy lookup.
    """

    def __init__(self) -> None:
        self._module: object | None = None

    def _resolve_module(self) -> object:
        if self._module is None:
            import huggingface_hub as _hub

            self._module = _hub
        return self._module

    def __getattr__(self, name: str) -> object:
        return getattr(self._resolve_module(), name)


_huggingface_hub = _HuggingFaceHub()


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
       on any mismatch / missing file / unauthenticated identity.

    The ``HfApi`` is resolved at invocation time (not at factory time), so
    tests may monkeypatch ``orch._huggingface_hub.HfApi`` BEFORE the
    verifier is actually called.
    """

    def _authenticated_api() -> Any:
        # Resolve the HfApi lazily at invocation time so monkeypatching
        # _huggingface_hub.HfApi is honored by tests.
        HfApiCls: Any = _huggingface_hub.HfApi
        api = HfApiCls()
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

    def _verify_files_at_revision(
        api: Any,
        repo_id: str,
        files: tuple[UploadItem, ...],
        revision: str,
    ) -> None:
        for item in files:
            try:
                entries = api.get_paths_info(
                    repo_id,
                    paths=[item.relative_path],
                    revision=revision,
                    repo_type="dataset",
                )
            except Exception as error:
                raise HubVerificationError(
                    f"hub verification failed for {item.relative_path}: {error}"
                ) from error
            entry = next((e for e in entries), None)
            if entry is None:
                raise HubVerificationError(
                    f"remote file missing in revision {revision}: {item.relative_path}"
                )
            size = getattr(entry, "size", None)
            if size is not None and int(size) != int(item.size_bytes):
                raise HubVerificationError(
                    f"remote size mismatch for {item.relative_path}: "
                    f"local={item.size_bytes}, remote={size}"
                )
            lfs_info = getattr(entry, "lfs", None)
            lfs_sha = getattr(lfs_info, "sha256", None) if lfs_info is not None else None
            if lfs_sha:
                if str(lfs_sha).lower() != str(item.sha256).lower():
                    raise HubVerificationError(f"remote LFS SHA mismatch for {item.relative_path}")
                continue
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
                local_path = api.hf_hub_download(
                    repo_id,
                    item.relative_path,
                    **download_kwargs,
                )
            except Exception as error:
                raise HubVerificationError(
                    f"could not download {item.relative_path} from {repo_id}@{revision}: {error}"
                ) from error
            digest = file_sha256(Path(local_path))
            if digest.lower() != str(item.sha256).lower():
                raise HubVerificationError(
                    f"remote SHA mismatch for {item.relative_path}: "
                    f"local={item.sha256}, remote={digest}"
                )

    def verifier(repo_id: str, files: tuple[UploadItem, ...]) -> str:
        api = _authenticated_api()
        revision = _repository_revision(api, repo_id)
        _verify_files_at_revision(api, repo_id, files, revision)
        return revision

    def verify_inventory(
        repo_id: str,
        files: tuple[UploadItem, ...],
        *,
        revision: str | None = None,
    ) -> str:
        api = _authenticated_api()
        resolved_revision = revision or _repository_revision(api, repo_id)
        try:
            remote_paths = set(
                api.list_repo_files(
                    repo_id,
                    revision=resolved_revision,
                    repo_type="dataset",
                )
            )
        except Exception as error:
            raise HubVerificationError(
                f"hub inventory lookup failed at revision {resolved_revision}: {error}"
            ) from error
        remote_inventory = {
            path
            for path in remote_paths
            if path.startswith("data/") or path.startswith("manifests/")
        }
        expected_inventory = {item.relative_path for item in files}
        if remote_inventory != expected_inventory:
            raise HubVerificationError(
                f"remote data/manifest inventory path mismatch at revision "
                f"{resolved_revision}: expected={sorted(expected_inventory)}, "
                f"remote={sorted(remote_inventory)}"
            )
        _verify_files_at_revision(api, repo_id, files, resolved_revision)
        return resolved_revision

    def reconcile_managed_files(repo_id: str, expected_paths: set[str]) -> str | None:
        """Delete only stale files in the dataset's managed artifact namespaces."""
        HfApiCls: Any = _huggingface_hub.HfApi
        api = HfApiCls()
        if not hasattr(api, "list_repo_files") or not hasattr(api, "delete_files"):
            return None
        remote_paths = set(api.list_repo_files(repo_id, repo_type="dataset"))
        stale = sorted(
            path
            for path in remote_paths - expected_paths
            if path.startswith("data/") or path.startswith("manifests/")
        )
        if not stale:
            return None
        commit = api.delete_files(
            repo_id,
            stale,
            repo_type="dataset",
            commit_message="Remove stale generated dataset artifacts",
        )
        revision = getattr(commit, "oid", None)
        if revision:
            return str(revision)
        info = api.repo_info(repo_id, repo_type="dataset")
        # pragma: no mutate start - missing SHA defaults are normalized below
        repo_sha = getattr(info, "sha", None)
        # pragma: no mutate end
        return str(repo_sha or "") or None

    class Verifier:
        def __call__(self, repo_id: str, files: tuple[UploadItem, ...]) -> str:
            return verifier(repo_id, files)

        def verify_inventory(
            self,
            repo_id: str,
            files: tuple[UploadItem, ...],
            *,
            revision: str | None = None,
        ) -> str:
            return verify_inventory(repo_id, files, revision=revision)

        def reconcile_managed_files(self, repo_id: str, expected_paths: set[str]) -> str | None:
            return reconcile_managed_files(repo_id, expected_paths)

    return Verifier()


def build_default_hub_verifier() -> HubVerifier:
    """Build a fresh default Hub verifier."""
    return default_hub_verifier_factory()


class HubVerificationError(RuntimeError):
    """Raised when the default Hub verifier cannot confirm the uploaded files."""
