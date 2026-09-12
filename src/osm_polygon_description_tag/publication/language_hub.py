"""The production ``LanguageHub`` implementation over ``huggingface_hub``.

The Hub reports a SHA-256 only for LFS-tracked blobs; for ordinary git blobs it
reports a git SHA-1 ``blob_id``, which is *not* a content SHA-256. Rather than
compare incomparable digests, small non-LFS files are downloaded and hashed
locally, and anything too large to hash that way is reported as unverifiable
instead of being quietly accepted.

``huggingface_hub`` is imported lazily so read-only and offline operations do
not pull in a network-authenticated dependency at import time.
"""

import hashlib
import math
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Final, cast

from osm_polygon_description_tag.publication.language import (
    LANGUAGE_REMOTE_PREFIX,
    LanguageExport,
    LanguagePublicationError,
    build_language_upload_plan,
    read_language_export,
)
from osm_polygon_description_tag.publication.language_card import install_language_card
from osm_polygon_description_tag.publication.language_upload import RemoteFile
from osm_polygon_description_tag.publication.models import UploadPlan
from osm_polygon_description_tag.publication.verification import _huggingface_hub

REPO_TYPE: Final = "dataset"
README_PATH: Final = "README.md"
DATASET_VIEWER_BASE_URL: Final = "https://datasets-server.huggingface.co"
DEFAULT_VIEWER_TIMEOUT_SECONDS: Final = 10.0
MAX_VIEWER_TIMEOUT_SECONDS: Final = 60.0
MAX_INLINE_HASH_BYTES: Final = 32 * 1024 * 1024
COMMIT_MESSAGE: Final = f"Add additive {LANGUAGE_REMOTE_PREFIX} language annotations"


class HuggingFaceLanguageHub:
    """Talk to one dataset repository for the additive language namespace only."""

    __slots__ = (
        "_api",
        "_cache_dir",
        "_http_session",
        "_viewer_base_url",
        "_viewer_timeout",
    )

    def __init__(
        self,
        api: Any = None,
        *,
        cache_dir: Path | None = None,
        http_session: Any = None,
        viewer_base_url: str = DATASET_VIEWER_BASE_URL,
        viewer_timeout: float = DEFAULT_VIEWER_TIMEOUT_SECONDS,
    ) -> None:
        self._api = api
        self._cache_dir = cache_dir
        self._http_session = http_session
        self._viewer_base_url = viewer_base_url.rstrip("/")
        self._viewer_timeout = _validated_viewer_timeout(viewer_timeout)

    @property
    def api(self) -> Any:
        """Return the ``HfApi`` instance, resolving it lazily."""
        if self._api is None:
            api_class: Any = _huggingface_hub.HfApi
            self._api = api_class()
        return self._api

    def repo_revision(self, repo_id: str) -> str:
        """Return the dataset repository's current commit SHA."""
        try:
            info = self.api.repo_info(repo_id, repo_type=REPO_TYPE)
        except Exception as error:
            raise LanguagePublicationError(
                f"Hub repository {repo_id} is not accessible: {error}"
            ) from error
        revision = str(getattr(info, "sha", "") or "")  # pragma: no mutate - same falsy default
        if not revision:
            raise LanguagePublicationError(f"Hub repository {repo_id} returned an empty revision")
        return revision

    def upload(self, plan: UploadPlan, *, parent_revision: str | None = None) -> None:
        """Commit exact language files and the derived card addition atomically."""
        paths = tuple(item.relative_path for item in plan.files)
        _require_additive(paths)
        root = Path(plan.data_root)
        export = read_language_export(root)
        _validate_direct_plan(plan, export)
        revision = (
            parent_revision if parent_revision is not None else self.repo_revision(plan.repo_id)
        )
        _require_revision(revision)
        baseline = self._readme(plan.repo_id, revision)
        updated = install_language_card(baseline, export)
        operations = [
            self._operation(item.relative_path, root / item.relative_path)
            for item in sorted(plan.files, key=lambda item: item.relative_path)
        ]
        readme_bytes = updated.encode("utf-8")  # pragma: no mutate - codec alias only
        operations.append(self._operation(README_PATH, readme_bytes))
        try:
            self.api.create_commit(
                repo_id=plan.repo_id,
                operations=operations,
                repo_type=REPO_TYPE,
                commit_message=COMMIT_MESSAGE,
                parent_commit=revision,
            )
        except Exception as error:
            raise LanguagePublicationError(
                f"cannot create the language publication commit for {plan.repo_id}: {error}"
            ) from error

    def _readme(self, repo_id: str, revision: str) -> str:
        try:
            local = self.api.hf_hub_download(**self._readme_kwargs(repo_id, revision))
            return _readme_text(local)
        except Exception as error:
            raise LanguagePublicationError(
                f"cannot read {README_PATH} at repository revision {revision}: {error}"
            ) from error

    def _readme_kwargs(self, repo_id: str, revision: str) -> dict[str, object]:
        kwargs: dict[str, object] = {
            "repo_id": repo_id,
            "filename": README_PATH,
            "repo_type": REPO_TYPE,
            "revision": revision,
        }
        if self._cache_dir is not None:
            kwargs["cache_dir"] = str(self._cache_dir)
        return kwargs

    def _operation(self, path: str, content: object) -> Any:
        try:
            operation_class: Any = _huggingface_hub.CommitOperationAdd
            return operation_class(path_in_repo=path, path_or_fileobj=content)
        except Exception as error:
            raise LanguagePublicationError(
                f"cannot prepare language publication file {path}: {error}"
            ) from error

    def paths_info(
        self, repo_id: str, revision: str, paths: Sequence[str]
    ) -> tuple[RemoteFile, ...]:
        """Return remote identities for ``paths`` at one exact revision."""
        _require_additive(paths)
        entries = self.api.get_paths_info(
            repo_id, list(paths), repo_type=REPO_TYPE, revision=revision
        )
        return tuple(
            self._remote_file(repo_id, revision, entry)
            for entry in entries
            if getattr(entry, "size", None) is not None
        )

    def _remote_file(self, repo_id: str, revision: str, entry: Any) -> RemoteFile:
        path = str(entry.path)
        size = int(entry.size)
        lfs = getattr(entry, "lfs", None)
        digest = getattr(lfs, "sha256", None)
        if isinstance(digest, str) and digest:
            return RemoteFile(path, size, digest)
        return RemoteFile(path, size, self._downloaded_sha256(repo_id, revision, path, size))

    def _downloaded_sha256(self, repo_id: str, revision: str, path: str, size: int) -> str:
        if size > MAX_INLINE_HASH_BYTES:
            raise LanguagePublicationError(
                f"remote file {path} is not LFS-tracked and is too large to verify by download"
            )
        local = self.api.hf_hub_download(
            repo_id=repo_id,
            filename=path,
            repo_type=REPO_TYPE,
            revision=revision,
            cache_dir=None if self._cache_dir is None else str(self._cache_dir),
        )
        return hashlib.sha256(Path(local).read_bytes()).hexdigest()

    def dataset_configs(self, repo_id: str, revision: str) -> tuple[str, ...]:
        """Return ready train configs from the unversioned Viewer endpoint.

        The endpoint is not revision-pinned. Returning no names when the Hub
        head differs, or when indexing is pending/failed, keeps publication
        verification conservative without treating card metadata as readiness.
        """
        _require_repo_id(repo_id)
        _require_revision(revision)
        if self.repo_revision(repo_id) != revision:
            return ()
        return self._ready_viewer_configs(repo_id)

    def _ready_viewer_configs(self, repo_id: str) -> tuple[str, ...]:
        try:
            observed = self.dataset_viewer_splits(repo_id)
        except LanguagePublicationError:
            return ()
        if observed.pending or observed.failed:
            return ()
        return _viewer_config_names(observed, repo_id)

    def dataset_viewer_splits(self, repo_id: str) -> "DatasetViewerSplits":
        """Read the unversioned Dataset Viewer ``/splits`` response."""
        _require_repo_id(repo_id)
        try:
            payload = self._viewer_payload(repo_id)
        except Exception as error:
            raise LanguagePublicationError(
                f"cannot read Dataset Viewer splits for {repo_id}: {error}"
            ) from error
        return _parse_viewer_splits(payload)

    def _viewer_payload(self, repo_id: str) -> object:
        response = self._viewer_http_session().get(
            f"{self._viewer_base_url}/splits",
            params={"dataset": repo_id},
            timeout=self._viewer_timeout,
        )
        _raise_for_viewer_status(response)
        return response.json()

    def _viewer_http_session(self) -> Any:
        if self._http_session is None:
            try:
                factory: Any = _huggingface_hub.get_session
                self._http_session = factory()
            except Exception as error:
                raise LanguagePublicationError(
                    f"cannot initialize Dataset Viewer HTTP session: {error}"
                ) from error
        return self._http_session


def _readme_text(local: str) -> str:
    path = Path(local)
    if not path.is_file():
        raise OSError("downloaded README is not a regular file")
    return path.read_bytes().decode("utf-8")  # pragma: no mutate - codec alias only


@dataclass(frozen=True, slots=True)
class DatasetViewerSplit:
    """One split advertised by the Dataset Viewer."""

    dataset: str
    config: str
    split: str


@dataclass(frozen=True, slots=True)
class DatasetViewerSplits:
    """The bounded, raw result of one Dataset Viewer ``/splits`` call."""

    splits: tuple[DatasetViewerSplit, ...]
    pending: tuple[object, ...]
    failed: tuple[object, ...]


def _viewer_config_names(observed: DatasetViewerSplits, repo_id: str) -> tuple[str, ...]:
    return tuple(
        dict.fromkeys(
            item.config
            for item in observed.splits
            if item.dataset == repo_id and item.split == "train"
        )
    )


def _validated_viewer_timeout(value: float) -> float:
    if isinstance(value, bool) or not isinstance(value, int | float):
        raise LanguagePublicationError("Dataset Viewer timeout must be a finite positive number")
    timeout = float(value)
    if not math.isfinite(timeout) or not 0 < timeout <= MAX_VIEWER_TIMEOUT_SECONDS:
        raise LanguagePublicationError(
            f"Dataset Viewer timeout must be between 0 and {MAX_VIEWER_TIMEOUT_SECONDS:g} seconds"
        )
    return timeout


def _require_revision(revision: str) -> None:
    if not isinstance(revision, str) or not revision:
        raise LanguagePublicationError("repository revision must be a non-empty string")


def _require_repo_id(repo_id: str) -> None:
    if not isinstance(repo_id, str) or not repo_id:
        raise LanguagePublicationError("Dataset Viewer repository id must not be empty")


def _raise_for_viewer_status(response: Any) -> None:
    raise_for_status = getattr(response, "raise_for_status", None)
    if callable(raise_for_status):
        raise_for_status()
        return
    status_code = getattr(response, "status_code", None)
    if not isinstance(status_code, int) or not 200 <= status_code < 300:
        raise RuntimeError(f"HTTP status {status_code}")


def _parse_viewer_splits(payload: object) -> DatasetViewerSplits:
    raw_splits, pending, failed = _viewer_lists(payload)
    splits = tuple(_parse_viewer_split(item) for item in raw_splits)
    return DatasetViewerSplits(splits, tuple(pending), tuple(failed))


def _viewer_lists(payload: object) -> tuple[list[object], list[object], list[object]]:
    if not isinstance(payload, dict):
        raise LanguagePublicationError("Dataset Viewer returned a malformed /splits response")
    raw_splits = payload.get("splits")
    pending = payload.get("pending")
    failed = payload.get("failed")
    if not all(isinstance(value, list) for value in (raw_splits, pending, failed)):
        raise LanguagePublicationError(
            "Dataset Viewer returned malformed splits, pending, or failed data"
        )
    typed_splits = cast(list[object], raw_splits)  # pragma: no mutate - static cast
    typed_pending = cast(list[object], pending)  # pragma: no mutate - static cast
    typed_failed = cast(list[object], failed)  # pragma: no mutate - static cast
    return (typed_splits, typed_pending, typed_failed)


def _parse_viewer_split(item: object) -> DatasetViewerSplit:
    if not isinstance(item, dict):
        raise LanguagePublicationError("Dataset Viewer returned a malformed split entry")
    dataset = item.get("dataset")
    config = item.get("config")
    split = item.get("split")
    if not all(isinstance(value, str) and value for value in (dataset, config, split)):
        raise LanguagePublicationError(
            "Dataset Viewer returned a malformed split without dataset/config/split"
        )
    dataset_value = cast(str, dataset)  # pragma: no mutate - static cast
    config_value = cast(str, config)  # pragma: no mutate - static cast
    split_value = cast(str, split)  # pragma: no mutate - static cast
    return DatasetViewerSplit(dataset_value, config_value, split_value)


def _validate_direct_plan(plan: UploadPlan, export: LanguageExport) -> None:
    if build_language_upload_plan(export, plan.repo_id, confirm_repo=plan.repo_id) != plan:
        raise LanguagePublicationError(
            "upload plan does not exactly match the validated language export"
        )


def _require_additive(paths: Sequence[str]) -> None:
    prefix = f"{LANGUAGE_REMOTE_PREFIX}/"
    seen: set[str] = set()
    for path in paths:
        _require_exact_path(path, prefix, seen)


def _require_exact_path(path: str, prefix: str, seen: set[str]) -> None:
    if not isinstance(path, str) or not path.startswith(prefix):
        raise LanguagePublicationError(
            f"the language hub may only touch {prefix} paths, refused: {path}"
        )
    if path in seen:
        raise LanguagePublicationError(f"the language hub received a duplicate path: {path}")
    seen.add(path)
    _require_safe_path(path)


def _require_safe_path(path: str) -> None:
    _require_path_structure(path)
    _require_no_glob(path)
    _require_no_control(path)


def _require_path_structure(path: str) -> None:
    if "\\" in path or any(part in {"", ".", ".."} for part in path.split("/")):
        raise LanguagePublicationError(
            f"the language hub requires exact safe file paths, refused: {path}"
        )


def _require_no_glob(path: str) -> None:
    if any(character in path for character in "*?[]{}"):
        raise LanguagePublicationError(
            f"the language hub requires exact safe file paths, refused: {path}"
        )


def _require_no_control(path: str) -> None:
    if any(ord(character) < 32 for character in path):
        raise LanguagePublicationError(
            f"the language hub requires exact safe file paths, refused: {path}"
        )


def build_language_hub(*, cache_dir: Path | None = None) -> HuggingFaceLanguageHub:
    """Return the production Hub adapter for the additive namespace."""
    return HuggingFaceLanguageHub(cache_dir=cache_dir)


__all__ = [
    "COMMIT_MESSAGE",
    "DATASET_VIEWER_BASE_URL",
    "DEFAULT_VIEWER_TIMEOUT_SECONDS",
    "MAX_INLINE_HASH_BYTES",
    "MAX_VIEWER_TIMEOUT_SECONDS",
    "README_PATH",
    "REPO_TYPE",
    "DatasetViewerSplit",
    "DatasetViewerSplits",
    "HuggingFaceLanguageHub",
    "build_language_hub",
]
