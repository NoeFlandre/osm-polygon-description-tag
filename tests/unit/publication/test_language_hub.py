"""The production Hub adapter, exercised entirely against fake Hub seams."""

import hashlib
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from osm_polygon_description_tag.dataset.languages.atomic import canonical_json_bytes
from osm_polygon_description_tag.publication import language_hub as language_hub_module
from osm_polygon_description_tag.publication.language import (
    LanguageExport,
    LanguagePublicationError,
    LanguageStats,
    build_language_upload_plan,
)
from osm_polygon_description_tag.publication.language_hub import (
    COMMIT_MESSAGE,
    DATASET_VIEWER_BASE_URL,
    MAX_INLINE_HASH_BYTES,
    DatasetViewerSplit,
    HuggingFaceLanguageHub,
    _raise_for_viewer_status,
    _readme_text,
    build_language_hub,
)
from osm_polygon_description_tag.publication.language_upload import (
    verify_language_publication,
)
from osm_polygon_description_tag.publication.models import UploadItem, UploadPlan
from tests.helpers.messages import exactly

REPO = "NoeFlandre/osm-polygon-description-tag"
DATA_PATH = "language-v1/data/region.parquet"
STATS_PATH = "language-v1/stats.json"
MANIFEST_PATH = "language-v1/export-manifest.json"


class _FakeApi:
    """A stand-in for ``HfApi`` that records calls and never uses the network."""

    def __init__(
        self,
        *,
        sha: str | None = "rev-1",
        entries: list[Any] | None = None,
        configs: Any = None,
        download: Path | None = None,
        repo_error: Exception | None = None,
        info_error: Exception | None = None,
    ) -> None:
        self._sha = sha
        self._entries = entries or []
        self._configs = configs
        self._download = download
        self._repo_error = repo_error
        self._info_error = info_error
        self.commits: list[dict[str, Any]] = []
        self.downloads: list[str] = []
        self.download_calls: list[dict[str, Any]] = []
        self.repo_info_calls: list[tuple[str, str]] = []

    def repo_info(self, repo_id: str, repo_type: str) -> Any:
        self.repo_info_calls.append((repo_id, repo_type))
        if self._repo_error is not None:
            raise self._repo_error
        return SimpleNamespace(sha=self._sha)

    def create_commit(self, **kwargs: Any) -> None:
        self.commits.append(kwargs)

    def get_paths_info(
        self, repo_id: str, paths: list[str], *, repo_type: str, revision: str
    ) -> list[Any]:
        return self._entries

    def hf_hub_download(self, **kwargs: Any) -> str:
        self.downloads.append(str(kwargs["filename"]))
        self.download_calls.append(kwargs)
        assert self._download is not None
        return str(self._download)

    def dataset_info(self, repo_id: str, revision: str) -> Any:
        if self._info_error is not None:
            raise self._info_error
        return SimpleNamespace(card_data=self._configs)


def _plan(
    paths: tuple[str, ...] = (DATA_PATH, STATS_PATH), *, data_root: str = "/srv/export"
) -> UploadPlan:
    return UploadPlan(
        repo_id=REPO,
        data_root=data_root,
        files=tuple(UploadItem(path, 10, "a" * 64) for path in paths),
        identity_sha256="b" * 64,
    )


def _synthetic_export(root: Path) -> LanguageExport:
    return LanguageExport(
        export_root=root,
        config_name="language-v1",
        snapshot_id="snapshot",
        model_config_fingerprint="config",
        library_name="detector",
        library_version="1.0",
        files=(DATA_PATH,),
        stats=LanguageStats(
            annotation_count=1,
            object_count=1,
            base_description_count=1,
            localized_description_count=0,
            detected_count=1,
            uncertain_count=0,
            non_linguistic_count=0,
            distinct_language_count=1,
            top_languages=(("eng", 1),),
        ),
    )


def _write_valid_export(root: Path) -> LanguageExport:
    export = _synthetic_export(root)
    data_path = root / DATA_PATH
    data_path.parent.mkdir(parents=True, exist_ok=True)
    data_path.write_bytes(b"x" * 10)
    stats_path = root / STATS_PATH
    stats_path.write_bytes(canonical_json_bytes(export.to_payload()))
    manifest = {
        "schema_version": 1,
        "files": [
            {
                "relative_path": relative_path,
                "size_bytes": (root / relative_path).stat().st_size,
                "sha256": hashlib.sha256((root / relative_path).read_bytes()).hexdigest(),
            }
            for relative_path in (DATA_PATH, STATS_PATH)
        ],
    }
    manifest_path = root / MANIFEST_PATH
    manifest_path.parent.mkdir(parents=True, exist_ok=True)
    manifest_path.write_bytes(canonical_json_bytes(manifest))
    return export


def _local_plan(root: Path, paths: tuple[str, ...]) -> UploadPlan:
    items = tuple(
        UploadItem(
            relative_path,
            (root / relative_path).stat().st_size,
            hashlib.sha256((root / relative_path).read_bytes()).hexdigest(),
        )
        for relative_path in paths
    )
    return UploadPlan(
        repo_id=REPO,
        data_root=str(root),
        files=items,
        identity_sha256="b" * 64,
    )


def _valid_plan(root: Path) -> UploadPlan:
    export = _write_valid_export(root)
    return build_language_upload_plan(export, REPO, confirm_repo=REPO)


class _FakeResponse:
    def __init__(self, payload: object, *, status_code: int = 200) -> None:
        self._payload = payload
        self.status_code = status_code

    def raise_for_status(self) -> None:
        if not 200 <= self.status_code < 300:
            raise RuntimeError(f"HTTP status {self.status_code}")

    def json(self) -> object:
        return self._payload


class _StatusOnlyResponse:
    def __init__(self, payload: object, *, status_code: int = 200) -> None:
        self._payload = payload
        self.status_code = status_code

    def json(self) -> object:
        return self._payload


class _FakeHttp:
    def __init__(self, response: Any) -> None:
        self.response = response
        self.calls: list[dict[str, Any]] = []

    def get(self, url: str, **kwargs: Any) -> _FakeResponse:
        self.calls.append({"url": url, **kwargs})
        return self.response


def _entry(path: str, size: int, *, lfs_sha: str | None = None) -> Any:
    lfs = None if lfs_sha is None else SimpleNamespace(sha256=lfs_sha)
    return SimpleNamespace(path=path, size=size, lfs=lfs, blob_id="git-sha1")


def test_the_current_revision_is_reported() -> None:
    api = _FakeApi(sha="abc123")

    assert HuggingFaceLanguageHub(api).repo_revision(REPO) == "abc123"
    assert api.repo_info_calls == [(REPO, "dataset")]


def test_a_missing_revision_attribute_is_rejected_as_an_empty_revision() -> None:
    class MissingShaApi(_FakeApi):
        def repo_info(self, repo_id: str, repo_type: str) -> Any:
            return SimpleNamespace()

    with pytest.raises(LanguagePublicationError) as caught:
        HuggingFaceLanguageHub(MissingShaApi()).repo_revision(REPO)

    assert str(caught.value) == f"Hub repository {REPO} returned an empty revision"


def test_an_inaccessible_repository_is_reported() -> None:
    hub = HuggingFaceLanguageHub(_FakeApi(repo_error=RuntimeError("403")))

    with pytest.raises(LanguagePublicationError, match="is not accessible"):
        hub.repo_revision(REPO)


@pytest.mark.parametrize("sha", [None, ""])
def test_an_empty_revision_is_rejected(sha: str | None) -> None:
    hub = HuggingFaceLanguageHub(_FakeApi(sha=sha))

    with pytest.raises(LanguagePublicationError, match="empty revision"):
        hub.repo_revision(REPO)


def test_upload_uses_one_atomic_commit_for_exact_files_and_readme(
    tmp_path: Path,
) -> None:
    root = tmp_path / "export"
    plan = _valid_plan(root)
    readme_source = tmp_path / "README-source.md"
    readme_source.write_text(
        "---\n"
        "configs:\n"
        "- config_name: default\n"
        "  data_files:\n"
        "  - split: train\n"
        "    path: data/*.parquet\n"
        "---\n\n"
        "# Existing prose\n",
        encoding="utf-8",
    )
    readme = tmp_path / "cache" / "snapshots" / "README.md"
    readme.parent.mkdir(parents=True)
    readme.symlink_to(readme_source)

    class NoFolderApi(_FakeApi):
        def upload_folder(self, **kwargs: Any) -> None:
            raise AssertionError("upload_folder must not be used")

    api = NoFolderApi(download=readme)
    cache_dir = tmp_path / "hf-cache"
    hub = HuggingFaceLanguageHub(api, cache_dir=cache_dir)

    hub.upload(plan)

    assert len(api.commits) == 1
    operations = api.commits[0]["operations"]
    assert [operation.path_in_repo for operation in operations] == [
        DATA_PATH,
        MANIFEST_PATH,
        STATS_PATH,
        "README.md",
    ]
    assert api.commits[0]["parent_commit"] == "rev-1"
    assert api.commits[0]["commit_message"] == COMMIT_MESSAGE
    assert api.download_calls[0] == {
        "repo_id": REPO,
        "filename": "README.md",
        "repo_type": "dataset",
        "revision": "rev-1",
        "cache_dir": str(cache_dir),
    }
    assert api.commits[0]["repo_id"] == REPO
    assert api.commits[0]["repo_type"] == "dataset"
    assert "delete_operations" not in api.commits[0]


def test_upload_refuses_any_path_outside_the_additive_namespace() -> None:
    api = _FakeApi()
    hub = HuggingFaceLanguageHub(api)

    with pytest.raises(LanguagePublicationError, match="may only touch language-v1/ paths"):
        hub.upload(_plan(("data/000.parquet",)))
    assert api.commits == []


def test_upload_binds_the_commit_to_the_preflight_revision(
    tmp_path: Path,
) -> None:
    root = tmp_path / "export"
    plan = _valid_plan(root)
    readme = tmp_path / "README.md"
    readme.write_text("---\nconfigs:\n- config_name: default\n---\n", encoding="utf-8")
    api = _FakeApi(sha="new-concurrent-revision", download=readme)
    hub = HuggingFaceLanguageHub(api)

    hub.upload(plan, parent_revision="approved-revision")

    assert api.commits[0]["parent_commit"] == "approved-revision"
    assert api.download_calls[0]["revision"] == "approved-revision"


def test_upload_resolves_the_plan_repository_when_no_parent_is_given(tmp_path: Path) -> None:
    root = tmp_path / "export"
    plan = _valid_plan(root)
    readme = tmp_path / "README.md"
    readme.write_text("---\nconfigs:\n- config_name: default\n---\n", encoding="utf-8")
    api = _FakeApi(download=readme)

    HuggingFaceLanguageHub(api).upload(plan)

    assert api.repo_info_calls == [(REPO, "dataset")]


def test_upload_reports_a_readme_download_failure(tmp_path: Path) -> None:
    plan = _valid_plan(tmp_path / "export")

    with pytest.raises(LanguagePublicationError, match="cannot read README"):
        HuggingFaceLanguageHub(_FakeApi()).upload(plan)


def test_upload_rejects_a_cached_readme_that_is_not_a_file(tmp_path: Path) -> None:
    plan = _valid_plan(tmp_path / "export")
    readme_directory = tmp_path / "README-directory"
    readme_directory.mkdir()
    api = _FakeApi(download=readme_directory)

    with pytest.raises(LanguagePublicationError, match="cannot read README"):
        HuggingFaceLanguageHub(api).upload(plan)

    assert api.commits == []


def test_upload_reports_a_commit_operation_failure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    plan = _valid_plan(tmp_path / "export")
    readme = tmp_path / "README.md"
    readme.write_text("---\nconfigs:\n- config_name: default\n---\n", encoding="utf-8")
    api = _FakeApi(download=readme)

    class FailingOperation:
        def __init__(self, **kwargs: object) -> None:
            raise RuntimeError("operation rejected")

    monkeypatch.setattr(
        language_hub_module._huggingface_hub,
        "CommitOperationAdd",
        FailingOperation,
        raising=False,
    )

    with pytest.raises(LanguagePublicationError, match="cannot prepare"):
        HuggingFaceLanguageHub(api).upload(plan)

    assert api.commits == []


def test_upload_reports_a_commit_api_failure(tmp_path: Path) -> None:
    plan = _valid_plan(tmp_path / "export")
    readme = tmp_path / "README.md"
    readme.write_text("---\nconfigs:\n- config_name: default\n---\n", encoding="utf-8")

    class FailingCommitApi(_FakeApi):
        def create_commit(self, **kwargs: Any) -> None:
            raise RuntimeError("commit rejected")

    api = FailingCommitApi(download=readme)

    with pytest.raises(LanguagePublicationError, match="cannot create"):
        HuggingFaceLanguageHub(api).upload(plan)

    assert api.commits == []


def test_upload_rejects_an_empty_parent_revision(tmp_path: Path) -> None:
    plan = _valid_plan(tmp_path / "export")

    with pytest.raises(LanguagePublicationError, match="revision"):
        HuggingFaceLanguageHub(_FakeApi()).upload(plan, parent_revision="")


def test_paths_info_refuses_a_non_additive_query() -> None:
    hub = HuggingFaceLanguageHub(_FakeApi())

    with pytest.raises(LanguagePublicationError, match="may only touch language-v1/ paths"):
        hub.paths_info(REPO, "rev-1", ["README.md"])


@pytest.mark.parametrize(
    "path",
    [
        "language-v1/data/*.parquet",
        "language-v1/../stats.json",
        "language-v1/data//region.parquet",
        "language-v1/data\\region.parquet",
        "language-v1/data/region.parquet?",
        "language-v1/data/region\x00.parquet",
    ],
)
def test_paths_info_refuses_globs_and_unsafe_exact_paths(path: str) -> None:
    hub = HuggingFaceLanguageHub(_FakeApi())

    with pytest.raises(LanguagePublicationError, match="exact safe file paths"):
        hub.paths_info(REPO, "rev-1", [path])


def test_paths_info_accepts_spaces_and_letter_x_in_an_exact_path() -> None:
    hub = HuggingFaceLanguageHub(_FakeApi())

    assert hub.paths_info(REPO, "rev-1", ["language-v1/data/X file.parquet"]) == ()


def test_paths_info_refuses_duplicate_paths() -> None:
    hub = HuggingFaceLanguageHub(_FakeApi())

    with pytest.raises(LanguagePublicationError, match="duplicate path"):
        hub.paths_info(REPO, "rev-1", [DATA_PATH, DATA_PATH])


def test_upload_refuses_a_plan_that_does_not_match_the_validated_export(
    tmp_path: Path,
) -> None:
    root = tmp_path / "export"
    _valid_plan(root)
    mismatched = _local_plan(root, (DATA_PATH, STATS_PATH))
    api = _FakeApi()

    with pytest.raises(LanguagePublicationError) as caught:
        HuggingFaceLanguageHub(api).upload(mismatched)

    assert str(caught.value) == "upload plan does not exactly match the validated language export"
    assert api.commits == []
    assert api.downloads == []


def test_upload_refuses_stale_plan_file_identity(
    tmp_path: Path,
) -> None:
    root = tmp_path / "export"
    valid = _valid_plan(root)
    stale = UploadPlan(
        repo_id=valid.repo_id,
        data_root=valid.data_root,
        files=tuple(
            UploadItem(item.relative_path, item.size_bytes, "0" * 64) for item in valid.files
        ),
        identity_sha256=valid.identity_sha256,
    )
    api = _FakeApi()

    with pytest.raises(LanguagePublicationError, match="exactly match"):
        HuggingFaceLanguageHub(api).upload(stale)

    assert api.commits == []
    assert api.downloads == []


def test_an_lfs_checksum_is_used_directly() -> None:
    api = _FakeApi(entries=[_entry(DATA_PATH, 4096, lfs_sha="c" * 64)])
    hub = HuggingFaceLanguageHub(api)

    remote = hub.paths_info(REPO, "rev-1", [DATA_PATH])

    assert remote == (type(remote[0])(DATA_PATH, 4096, "c" * 64),)
    assert api.downloads == []


def test_a_small_non_lfs_file_is_downloaded_and_hashed(tmp_path: Path) -> None:
    blob = tmp_path / "stats.json"
    blob.write_bytes(b'{"annotation_count": 3}')
    api = _FakeApi(entries=[_entry(STATS_PATH, blob.stat().st_size)], download=blob)
    hub = HuggingFaceLanguageHub(api)

    remote = hub.paths_info(REPO, "rev-1", [STATS_PATH])

    assert (remote[0].relative_path, remote[0].size_bytes) == (STATS_PATH, blob.stat().st_size)
    assert remote[0].sha256 == hashlib.sha256(blob.read_bytes()).hexdigest()
    assert api.downloads == [STATS_PATH]


def test_a_large_non_lfs_file_is_reported_as_unverifiable() -> None:
    api = _FakeApi(entries=[_entry(STATS_PATH, MAX_INLINE_HASH_BYTES + 1)])
    hub = HuggingFaceLanguageHub(api)

    with pytest.raises(LanguagePublicationError, match="too large to verify by download"):
        hub.paths_info(REPO, "rev-1", [STATS_PATH])


def test_directories_are_ignored(tmp_path: Path) -> None:
    folder = SimpleNamespace(path="language-v1/data", size=None)
    api = _FakeApi(entries=[folder, _entry(DATA_PATH, 10, lfs_sha="d" * 64)])
    hub = HuggingFaceLanguageHub(api)

    remote = hub.paths_info(REPO, "rev-1", [DATA_PATH])

    assert [item.relative_path for item in remote] == [DATA_PATH]


def test_an_empty_lfs_checksum_falls_back_to_downloading(tmp_path: Path) -> None:
    blob = tmp_path / "blob"
    blob.write_bytes(b"content")
    api = _FakeApi(entries=[_entry(DATA_PATH, 7, lfs_sha="")], download=blob)
    hub = HuggingFaceLanguageHub(api)

    remote = hub.paths_info(REPO, "rev-1", [DATA_PATH])

    assert remote[0].sha256 == hashlib.sha256(b"content").hexdigest()


def test_dataset_configuration_names_are_read_from_the_viewer() -> None:
    response = _FakeResponse(
        {
            "splits": [
                {"dataset": REPO, "config": "default", "split": "train"},
                {"dataset": REPO, "config": "language-v1", "split": "train"},
            ],
            "pending": [],
            "failed": [],
        }
    )
    http = _FakeHttp(response)
    hub = HuggingFaceLanguageHub(_FakeApi(sha="rev-1"), http_session=http)

    assert hub.dataset_configs(REPO, "rev-1") == ("default", "language-v1")
    assert len(http.calls) == 1


def test_dataset_configs_requires_the_exact_dataset_and_train_split() -> None:
    response = _FakeResponse(
        {
            "splits": [
                {"dataset": "other/dataset", "config": "language-v1", "split": "train"},
                {"dataset": REPO, "config": "language-v1", "split": "test"},
            ],
            "pending": [],
            "failed": [],
        }
    )
    http = _FakeHttp(response)
    hub = HuggingFaceLanguageHub(_FakeApi(sha="rev-1"), http_session=http)

    assert hub.dataset_configs(REPO, "rev-1") == ()
    assert len(http.calls) == 1


def test_publication_verification_uses_viewer_configs_from_the_real_adapter() -> None:
    response = _FakeResponse(
        {
            "splits": [{"dataset": REPO, "config": "language-v1", "split": "train"}],
            "pending": [],
            "failed": [],
        }
    )
    http = _FakeHttp(response)
    api = _FakeApi(entries=[_entry(DATA_PATH, 10, lfs_sha="a" * 64)])
    hub = HuggingFaceLanguageHub(api, http_session=http)

    verified, issues = verify_language_publication(_plan((DATA_PATH,)), hub, revision="rev-1")

    assert verified == (DATA_PATH,)
    assert issues == ()
    assert len(http.calls) == 1


def test_dataset_viewer_splits_uses_the_injected_http_session() -> None:
    response = _FakeResponse(
        {
            "splits": [{"dataset": REPO, "config": "language-v1", "split": "train"}],
            "pending": [],
            "failed": [],
        }
    )
    http = _FakeHttp(response)
    hub = HuggingFaceLanguageHub(_FakeApi(), http_session=http, viewer_timeout=2.5)

    observed = hub.dataset_viewer_splits(REPO)

    assert observed.splits == (DatasetViewerSplit(REPO, "language-v1", "train"),)
    assert observed.pending == ()
    assert observed.failed == ()
    assert http.calls == [
        {
            "url": f"{DATASET_VIEWER_BASE_URL}/splits",
            "params": {"dataset": REPO},
            "timeout": 2.5,
        }
    ]


def test_dataset_configs_does_not_treat_card_metadata_as_viewer_readiness() -> None:
    card = SimpleNamespace(configs=[{"config_name": "language-v1"}])
    response = _FakeResponse(
        {
            "splits": [{"dataset": REPO, "config": "default", "split": "train"}],
            "pending": [],
            "failed": [],
        }
    )
    http = _FakeHttp(response)
    hub = HuggingFaceLanguageHub(_FakeApi(configs=card), http_session=http)

    assert hub.dataset_configs(REPO, "rev-1") == ("default",)
    assert len(http.calls) == 1


def test_dataset_configs_returns_empty_while_viewer_indexing_is_pending_or_failed() -> None:
    response = _FakeResponse(
        {
            "splits": [{"dataset": REPO, "config": "language-v1", "split": "train"}],
            "pending": [{"dataset": REPO, "config": "language-v1"}],
            "failed": [{"dataset": REPO, "config": "language-v1"}],
        }
    )
    http = _FakeHttp(response)
    hub = HuggingFaceLanguageHub(_FakeApi(sha="rev-1"), http_session=http)

    assert hub.dataset_configs(REPO, "rev-1") == ()
    assert len(http.calls) == 1


@pytest.mark.parametrize(
    "payload",
    [
        {
            "splits": [{"dataset": REPO, "config": "language-v1", "split": "train"}],
            "pending": [{"dataset": REPO}],
            "failed": [],
        },
        {
            "splits": [{"dataset": REPO, "config": "language-v1", "split": "train"}],
            "pending": [],
            "failed": [{"dataset": REPO}],
        },
    ],
)
def test_dataset_configs_returns_empty_for_either_pending_or_failed_viewer_state(
    payload: dict[str, object],
) -> None:
    http = _FakeHttp(_FakeResponse(payload))
    hub = HuggingFaceLanguageHub(_FakeApi(sha="rev-1"), http_session=http)

    assert hub.dataset_configs(REPO, "rev-1") == ()


def test_dataset_configs_refuses_a_revision_when_the_head_has_drifted() -> None:
    http = _FakeHttp(_FakeResponse({"splits": [], "pending": [], "failed": []}))
    hub = HuggingFaceLanguageHub(_FakeApi(sha="head-2"), http_session=http)

    assert hub.dataset_configs(REPO, "head-1") == ()
    assert http.calls == []


def test_dataset_configs_checks_the_requested_repository_revision() -> None:
    class RecordingApi(_FakeApi):
        def __init__(self) -> None:
            super().__init__(sha="rev-1")
            self.revisions: list[str] = []

        def repo_info(self, repo_id: str, repo_type: str) -> Any:
            self.revisions.append(repo_id)
            return super().repo_info(repo_id, repo_type)

    api = RecordingApi()
    http = _FakeHttp(_FakeResponse({"splits": [], "pending": [], "failed": []}))

    assert HuggingFaceLanguageHub(api, http_session=http).dataset_configs(REPO, "rev-1") == ()
    assert api.revisions == [REPO]


def test_dataset_configs_returns_empty_for_a_malformed_viewer_response() -> None:
    http = _FakeHttp(_FakeResponse({"splits": [], "pending": []}))
    hub = HuggingFaceLanguageHub(_FakeApi(sha="rev-1"), http_session=http)

    assert hub.dataset_configs(REPO, "rev-1") == ()
    assert len(http.calls) == 1


def test_dataset_configs_returns_empty_for_a_viewer_http_failure() -> None:
    http = _FakeHttp(_FakeResponse({}, status_code=503))
    hub = HuggingFaceLanguageHub(_FakeApi(sha="rev-1"), http_session=http)

    assert hub.dataset_configs(REPO, "rev-1") == ()
    assert len(http.calls) == 1


@pytest.mark.parametrize(
    "payload",
    [
        None,
        {},
        {"splits": [], "pending": []},
        {"splits": [None], "pending": [], "failed": []},
        {"splits": [{"dataset": REPO}], "pending": [], "failed": []},
    ],
)
def test_dataset_viewer_rejects_malformed_responses(payload: object) -> None:
    hub = HuggingFaceLanguageHub(_FakeApi(), http_session=_FakeHttp(_FakeResponse(payload)))

    with pytest.raises(LanguagePublicationError, match="malformed"):
        hub.dataset_viewer_splits(REPO)


def test_dataset_viewer_reports_http_failure() -> None:
    hub = HuggingFaceLanguageHub(
        _FakeApi(), http_session=_FakeHttp(_FakeResponse({}, status_code=503))
    )

    with pytest.raises(LanguagePublicationError, match="cannot read Dataset Viewer splits"):
        hub.dataset_viewer_splits(REPO)


def test_dataset_viewer_accepts_a_status_only_http_response() -> None:
    payload = {
        "splits": [{"dataset": REPO, "config": "language-v1", "split": "train"}],
        "pending": [],
        "failed": [],
    }
    hub = HuggingFaceLanguageHub(_FakeApi(), http_session=_FakeHttp(_StatusOnlyResponse(payload)))

    assert hub.dataset_viewer_splits(REPO).splits == (
        DatasetViewerSplit(REPO, "language-v1", "train"),
    )


def test_dataset_viewer_rejects_a_status_only_http_error() -> None:
    hub = HuggingFaceLanguageHub(
        _FakeApi(), http_session=_FakeHttp(_StatusOnlyResponse({}, status_code=503))
    )

    with pytest.raises(LanguagePublicationError, match="cannot read Dataset Viewer splits"):
        hub.dataset_viewer_splits(REPO)


def test_raise_for_viewer_status_prefers_the_response_method() -> None:
    called: list[str] = []

    class Response:
        status_code = 503

        def raise_for_status(self) -> None:
            called.append("called")

    _raise_for_viewer_status(Response())

    assert called == ["called"]


@pytest.mark.parametrize(
    "response",
    [SimpleNamespace(status_code=300), SimpleNamespace()],
)
def test_raise_for_viewer_status_rejects_non_success_statuses_exactly(
    response: object,
) -> None:
    with pytest.raises(RuntimeError) as caught:
        _raise_for_viewer_status(response)

    status_code = getattr(response, "status_code", None)
    assert str(caught.value) == f"HTTP status {status_code}"


def test_dataset_viewer_uses_the_lazy_http_session_factory(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    response = _FakeResponse({"splits": [], "pending": [], "failed": []})
    http = _FakeHttp(response)
    monkeypatch.setattr(
        language_hub_module._huggingface_hub,
        "get_session",
        lambda: http,
        raising=False,
    )

    assert HuggingFaceLanguageHub(_FakeApi()).dataset_viewer_splits(REPO).splits == ()


def test_dataset_viewer_reports_a_session_factory_failure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def fail() -> Any:
        raise RuntimeError("session unavailable")

    monkeypatch.setattr(language_hub_module._huggingface_hub, "get_session", fail, raising=False)

    with pytest.raises(LanguagePublicationError, match="cannot read Dataset Viewer splits"):
        HuggingFaceLanguageHub(_FakeApi()).dataset_viewer_splits(REPO)


def test_dataset_viewer_session_factory_failure_keeps_its_exact_cause(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def fail() -> Any:
        raise RuntimeError("session unavailable")

    monkeypatch.setattr(language_hub_module._huggingface_hub, "get_session", fail, raising=False)

    with pytest.raises(LanguagePublicationError) as caught:
        HuggingFaceLanguageHub(_FakeApi())._viewer_http_session()

    assert str(caught.value) == "cannot initialize Dataset Viewer HTTP session: session unavailable"


@pytest.mark.parametrize(
    ("timeout", "message"),
    [
        (0, "Dataset Viewer timeout must be between 0 and 60 seconds"),
        (61, "Dataset Viewer timeout must be between 0 and 60 seconds"),
        (float("inf"), "Dataset Viewer timeout must be between 0 and 60 seconds"),
        (True, "Dataset Viewer timeout must be a finite positive number"),
        ("2", "Dataset Viewer timeout must be a finite positive number"),
    ],
)
def test_dataset_viewer_timeout_is_bounded(timeout: object, message: str) -> None:
    """A non-number and an out-of-range number are refused for different reasons."""
    with pytest.raises(LanguagePublicationError, match=exactly(message)):
        HuggingFaceLanguageHub(_FakeApi(), viewer_timeout=timeout)  # type: ignore[arg-type]


def test_a_fractional_positive_viewer_timeout_is_accepted() -> None:
    hub = HuggingFaceLanguageHub(_FakeApi(), viewer_timeout=0.5)

    assert hub._viewer_timeout == 0.5


def test_dataset_viewer_rejects_an_empty_repository_id() -> None:
    with pytest.raises(
        LanguagePublicationError, match=exactly("Dataset Viewer repository id must not be empty")
    ):
        HuggingFaceLanguageHub(
            _FakeApi(),
            http_session=_FakeHttp(_FakeResponse({"splits": [], "pending": [], "failed": []})),
        ).dataset_viewer_splits("")


def test_dataset_configs_rejects_an_empty_revision() -> None:
    with pytest.raises(LanguagePublicationError) as caught:
        HuggingFaceLanguageHub(_FakeApi()).dataset_configs(REPO, "")

    assert str(caught.value) == "repository revision must be a non-empty string"


def test_readme_text_reports_the_exact_non_file_cause(tmp_path: Path) -> None:
    directory = tmp_path / "README-directory"
    directory.mkdir()

    with pytest.raises(OSError) as caught:
        _readme_text(str(directory))

    assert str(caught.value) == "downloaded README is not a regular file"


def test_the_api_is_resolved_lazily(monkeypatch: pytest.MonkeyPatch) -> None:
    from osm_polygon_description_tag.publication import verification

    created: list[str] = []

    class _Api:
        def __init__(self) -> None:
            created.append("built")

    monkeypatch.setattr(verification._huggingface_hub, "HfApi", _Api, raising=False)
    hub = build_language_hub()

    assert created == []
    assert isinstance(hub.api, _Api)
    assert hub.api is hub.api
    assert created == ["built"]


def test_build_language_hub_preserves_the_requested_cache_directory(tmp_path: Path) -> None:
    cache_dir = tmp_path / "hf-cache"

    hub = build_language_hub(cache_dir=cache_dir)

    assert hub._cache_dir == cache_dir
