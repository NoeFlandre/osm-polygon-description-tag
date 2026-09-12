"""Exact arguments the Hub adapter sends, and exact refusals it raises.

Verification is only meaningful if it asks the Hub about the right repository at
the right revision. These tests record what the adapter actually passes to
`huggingface_hub` and assert it by value; none of them touch the network.
"""

from __future__ import annotations

import hashlib
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from osm_polygon_description_tag.publication.language import LanguagePublicationError
from osm_polygon_description_tag.publication.language_hub import (
    MAX_INLINE_HASH_BYTES,
    MAX_VIEWER_TIMEOUT_SECONDS,
    REPO_TYPE,
    DatasetViewerSplit,
    HuggingFaceLanguageHub,
    _parse_viewer_splits,
    _require_no_control,
    _require_no_glob,
    _require_path_structure,
)

_REPO = "NoeFlandre/osm-polygon-description-tag"
_REVISION = "a" * 40
_PATH = "language-v1/data/region.parquet"


class _Api:
    """An HfApi stand-in that records every call it receives."""

    def __init__(self, *, entries: list[Any] | None = None, download: Path | None = None) -> None:
        self.entries = entries or []
        self.download = download
        self.calls: list[tuple[str, tuple[Any, ...], dict[str, Any]]] = []

    def get_paths_info(self, *args: Any, **kwargs: Any) -> list[Any]:
        self.calls.append(("get_paths_info", args, kwargs))
        return self.entries

    def hf_hub_download(self, *args: Any, **kwargs: Any) -> str:
        self.calls.append(("hf_hub_download", args, kwargs))
        assert self.download is not None
        return str(self.download)

    def repo_info(self, *args: Any, **kwargs: Any) -> Any:
        self.calls.append(("repo_info", args, kwargs))
        return SimpleNamespace(sha=_REVISION)


def _entry(path: str, size: int, digest: str | None = None) -> Any:
    lfs = None if digest is None else SimpleNamespace(sha256=digest)
    return SimpleNamespace(path=path, size=size, lfs=lfs)


def test_paths_info_asks_for_the_exact_repo_paths_type_and_revision() -> None:
    api = _Api(entries=[_entry(_PATH, 10, "b" * 64)])
    hub = HuggingFaceLanguageHub(api)

    files = hub.paths_info(_REPO, _REVISION, [_PATH])

    name, args, kwargs = api.calls[0]
    assert name == "get_paths_info"
    assert args == (_REPO, [_PATH])
    assert kwargs == {"repo_type": REPO_TYPE, "revision": _REVISION}
    assert len(files) == 1
    assert (files[0].relative_path, files[0].size_bytes, files[0].sha256) == (_PATH, 10, "b" * 64)


def test_an_entry_without_a_size_is_not_reported_as_a_remote_file() -> None:
    api = _Api(entries=[SimpleNamespace(path=_PATH, size=None, lfs=None)])

    assert HuggingFaceLanguageHub(api).paths_info(_REPO, _REVISION, [_PATH]) == ()


def test_an_entry_without_a_size_attribute_is_not_reported_as_a_remote_file() -> None:
    api = _Api(entries=[SimpleNamespace(path=_PATH, lfs=None)])

    assert HuggingFaceLanguageHub(api).paths_info(_REPO, _REVISION, [_PATH]) == ()


def test_a_non_lfs_file_is_downloaded_at_the_same_repo_and_revision(tmp_path: Path) -> None:
    blob = tmp_path / "blob"
    blob.write_bytes(b"payload")
    api = _Api(entries=[_entry(_PATH, 7)], download=blob)
    hub = HuggingFaceLanguageHub(api, cache_dir=tmp_path / "cache")

    files = hub.paths_info(_REPO, _REVISION, [_PATH])

    _, _, kwargs = api.calls[1]
    assert kwargs == {
        "repo_id": _REPO,
        "filename": _PATH,
        "repo_type": REPO_TYPE,
        "revision": _REVISION,
        "cache_dir": str(tmp_path / "cache"),
    }
    assert files[0].sha256 == hashlib.sha256(b"payload").hexdigest()


def test_a_non_lfs_file_without_an_lfs_attribute_is_downloaded(tmp_path: Path) -> None:
    blob = tmp_path / "blob"
    blob.write_bytes(b"payload")
    api = _Api(entries=[SimpleNamespace(path=_PATH, size=7)], download=blob)

    files = HuggingFaceLanguageHub(api).paths_info(_REPO, _REVISION, [_PATH])

    assert (files[0].relative_path, files[0].size_bytes) == (_PATH, 7)
    assert files[0].sha256 == hashlib.sha256(b"payload").hexdigest()


def test_readme_download_arguments_are_exact() -> None:
    hub = HuggingFaceLanguageHub(_Api(), cache_dir=Path("/cache"))

    assert hub._readme_kwargs(_REPO, _REVISION) == {
        "repo_id": _REPO,
        "filename": "README.md",
        "repo_type": REPO_TYPE,
        "revision": _REVISION,
        "cache_dir": "/cache",
    }


def test_path_guards_allow_safe_boundary_characters() -> None:
    path = "language-v1/data/X file.parquet"

    _require_path_structure(path)
    _require_no_glob(path)
    _require_no_control(path)


def test_path_structure_rejects_a_single_dot_segment() -> None:
    with pytest.raises(LanguagePublicationError) as caught:
        _require_path_structure("language-v1/./region.parquet")

    assert str(caught.value) == (
        "the language hub requires exact safe file paths, refused: language-v1/./region.parquet"
    )


def test_no_cache_dir_is_passed_through_as_none(tmp_path: Path) -> None:
    blob = tmp_path / "blob"
    blob.write_bytes(b"payload")
    api = _Api(entries=[_entry(_PATH, 7)], download=blob)

    HuggingFaceLanguageHub(api).paths_info(_REPO, _REVISION, [_PATH])

    assert api.calls[1][2]["cache_dir"] is None


def test_a_non_lfs_file_at_the_inline_limit_is_still_verified(tmp_path: Path) -> None:
    """The limit is exclusive, so a file exactly at it must not be refused."""
    blob = tmp_path / "blob"
    blob.write_bytes(b"x")
    api = _Api(entries=[_entry(_PATH, MAX_INLINE_HASH_BYTES)], download=blob)

    files = HuggingFaceLanguageHub(api).paths_info(_REPO, _REVISION, [_PATH])

    assert files[0].sha256 == hashlib.sha256(b"x").hexdigest()


def test_a_non_lfs_file_above_the_inline_limit_is_refused_by_name() -> None:
    api = _Api(entries=[_entry(_PATH, MAX_INLINE_HASH_BYTES + 1)])

    with pytest.raises(LanguagePublicationError) as caught:
        HuggingFaceLanguageHub(api).paths_info(_REPO, _REVISION, [_PATH])

    assert str(caught.value) == (
        f"remote file {_PATH} is not LFS-tracked and is too large to verify by download"
    )


def test_an_empty_repository_revision_is_refused_exactly() -> None:
    class _Empty(_Api):
        def repo_info(self, *args: Any, **kwargs: Any) -> Any:
            return SimpleNamespace(sha="")

    with pytest.raises(LanguagePublicationError) as caught:
        HuggingFaceLanguageHub(_Empty()).repo_revision(_REPO)

    assert str(caught.value) == f"Hub repository {_REPO} returned an empty revision"


def test_configs_are_empty_when_the_head_moved_away_from_the_verified_revision() -> None:
    """The Viewer endpoint is unversioned, so a moved head means no evidence."""
    hub = HuggingFaceLanguageHub(_Api())

    assert hub.dataset_configs(_REPO, "b" * 40) == ()


_RANGE_MESSAGE = (
    f"Dataset Viewer timeout must be between 0 and {MAX_VIEWER_TIMEOUT_SECONDS:g} seconds"
)


@pytest.mark.parametrize(
    ("timeout", "message"),
    [
        (True, "Dataset Viewer timeout must be a finite positive number"),
        ("10", "Dataset Viewer timeout must be a finite positive number"),
        (None, "Dataset Viewer timeout must be a finite positive number"),
        (0, _RANGE_MESSAGE),
        (-1.0, _RANGE_MESSAGE),
        (float("inf"), _RANGE_MESSAGE),
        (float("nan"), _RANGE_MESSAGE),
        (MAX_VIEWER_TIMEOUT_SECONDS + 1, _RANGE_MESSAGE),
    ],
)
def test_an_invalid_viewer_timeout_is_refused_exactly(timeout: object, message: str) -> None:
    with pytest.raises(LanguagePublicationError) as caught:
        HuggingFaceLanguageHub(_Api(), viewer_timeout=timeout)  # type: ignore[arg-type]

    assert str(caught.value) == message


def test_the_largest_allowed_viewer_timeout_is_kept_verbatim() -> None:
    """The upper bound is inclusive, so it must be stored, not refused."""
    hub = HuggingFaceLanguageHub(_Api(), viewer_timeout=MAX_VIEWER_TIMEOUT_SECONDS)

    assert hub._viewer_timeout == MAX_VIEWER_TIMEOUT_SECONDS


def test_only_a_trailing_slash_is_stripped_from_the_viewer_base_url() -> None:
    hub = HuggingFaceLanguageHub(_Api(), viewer_base_url="https://example.test/api/")

    assert hub._viewer_base_url == "https://example.test/api"


def test_a_letter_x_at_the_end_of_the_viewer_base_url_is_not_stripped() -> None:
    hub = HuggingFaceLanguageHub(_Api(), viewer_base_url="https://example.test/apiX")

    assert hub._viewer_base_url == "https://example.test/apiX"


@pytest.mark.parametrize("payload", [[], "x", None, 7])
def test_a_non_object_splits_response_is_refused_exactly(payload: object) -> None:
    with pytest.raises(LanguagePublicationError) as caught:
        _parse_viewer_splits(payload)

    assert str(caught.value) == "Dataset Viewer returned a malformed /splits response"


@pytest.mark.parametrize(
    "payload",
    [
        {"splits": [], "pending": [], "failed": None},
        {"splits": [], "pending": None, "failed": []},
        {"splits": None, "pending": [], "failed": []},
        {"splits": [], "pending": []},
    ],
)
def test_splits_pending_or_failed_that_are_not_lists_are_refused_exactly(
    payload: dict[str, object],
) -> None:
    with pytest.raises(LanguagePublicationError) as caught:
        _parse_viewer_splits(payload)

    assert str(caught.value) == "Dataset Viewer returned malformed splits, pending, or failed data"


def test_a_split_entry_that_is_not_an_object_is_refused_exactly() -> None:
    with pytest.raises(LanguagePublicationError) as caught:
        _parse_viewer_splits({"splits": ["x"], "pending": [], "failed": []})

    assert str(caught.value) == "Dataset Viewer returned a malformed split entry"


@pytest.mark.parametrize(
    "entry",
    [
        {"dataset": "d", "config": "c"},
        {"dataset": "d", "config": "c", "split": ""},
        {"dataset": "d", "config": 7, "split": "train"},
    ],
)
def test_a_split_missing_any_of_the_three_names_is_refused_exactly(
    entry: dict[str, object],
) -> None:
    with pytest.raises(LanguagePublicationError) as caught:
        _parse_viewer_splits({"splits": [entry], "pending": [], "failed": []})

    assert str(caught.value) == (
        "Dataset Viewer returned a malformed split without dataset/config/split"
    )


def test_a_complete_split_entry_keeps_its_three_names_in_order() -> None:
    observed = _parse_viewer_splits(
        {
            "splits": [{"dataset": _REPO, "config": "language-v1", "split": "train"}],
            "pending": [],
            "failed": [],
        }
    )

    assert observed.splits == (DatasetViewerSplit(_REPO, "language-v1", "train"),)
