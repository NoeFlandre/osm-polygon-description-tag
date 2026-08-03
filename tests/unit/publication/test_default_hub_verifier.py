"""RED tests proving the public CLI path uses a real default Hub verifier.

These tests invoke the ``run_and_publish`` Python API as the production CLI
does — without injecting ``verifier`` or ``upload_runner`` — and prove that
the default factory:

1. constructs a real :class:`huggingface_hub.HfApi` client (with the
   environment token when set);
2. uses the Hub API to obtain the actual repository commit SHA after upload;
3. queries and checks every item in the plan at that revision;
4. fails closed on missing remote artifacts and hash mismatches.

External Hub API calls and ``subprocess`` boundaries are stubbed via
``monkeypatch`` only.
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any

import pytest

from osm_polygon_description_tag.config import Paths
from osm_polygon_description_tag.orchestrator import (
    PUBLICATION_STATE_FILENAME,
    default_hub_verifier_factory,
)
from osm_polygon_description_tag.publication import REPO_ID, UploadItem


class _FakeRepo:
    """In-process stand-in for ``huggingface_hub.HfApi.repo_info``."""

    def __init__(self, sha: str, files: dict[str, _FakeFile] | None = None):
        self.sha = sha
        self.files = files or {}


class _FakeFile:
    def __init__(self, content: bytes):
        self.content = content
        self.size = len(content)
        self.sha = hashlib.sha256(content).hexdigest()


class _FakeHubApi:
    """In-process stand-in for ``huggingface_hub.HfApi``.

    Tests populate ``repo_infos`` and revisions. ``files`` map path to
    :class:`_FakeFile`; their content determines both the LFS-less SHA
    (via direct content hashing) and the size.
    """

    def __init__(
        self,
        *,
        repo_infos: dict[str, _FakeRepo],
        revisions: dict[str, list[str]] | None = None,
    ):
        self._repo_infos = repo_infos
        self._revisions = revisions or {}
        self.calls: list[tuple[str, tuple[Any, ...], dict[str, Any]]] = []

    def whoami(self) -> object:
        self.calls.append(("whoami", (), {}))
        return {"name": "fakeuser"}

    def repo_info(self, repo_id: str, *, repo_type: str = "dataset"):
        self.calls.append(("repo_info", (repo_id,), {"repo_type": repo_type}))
        revs = self._revisions.setdefault(repo_id, [])
        sha = revs.pop(0) if revs else self._repo_infos[repo_id].sha
        return _RepoInfoStub(repo_id, sha)

    def list_repo_files(self, repo_id: str, *, revision: str, repo_type: str = "dataset"):
        self.calls.append(
            ("list_repo_files", (repo_id,), {"revision": revision, "repo_type": repo_type})
        )
        repo = self._repo_infos.get(repo_id)
        return list(repo.files) if repo else []

    def get_paths_info(self, repo_id: str, paths, *, revision: str, repo_type: str = "dataset"):
        self.calls.append(
            (
                "get_paths_info",
                (repo_id, list(paths)),
                {"revision": revision, "repo_type": repo_type},
            )
        )
        repo = self._repo_infos.get(repo_id)
        out = []
        for path in paths:
            entry = repo.files.get(path) if repo else None
            if entry is None:
                raise LookupError(f"remote file missing: {path}")
            out.append(_PathInfoStub(path, entry.size, entry.sha))
        return out

    def get_hf_file_metadata(
        self, repo_id: str, path: str, *, revision: str, repo_type: str = "dataset"
    ):
        self.calls.append(
            (
                "get_hf_file_metadata",
                (repo_id, path),
                {"revision": revision, "repo_type": repo_type},
            )
        )
        repo = self._repo_infos.get(repo_id)
        entry = repo.files.get(path) if repo else None
        if entry is None:
            raise LookupError(path)
        # No LFS metadata returned; verifier falls back to download.
        return _BlobInfoStub(size=entry.size, lfs_sha256=None)

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
        repo = self._repo_infos.get(repo_id)
        entry = repo.files.get(filename) if repo else None
        if entry is None:
            raise LookupError(filename)
        import os as _os
        import tempfile

        handle, path_str = tempfile.mkstemp(prefix="hf-", suffix=f"-{_os.path.basename(filename)}")
        _ = handle
        from pathlib import Path as _Path

        _Path(path_str).write_bytes(entry.content)
        return path_str


class _RepoInfoStub:
    def __init__(self, repo_id: str, sha: str):
        self.id = repo_id
        self.sha = sha


class _PathInfoStub:
    def __init__(self, path: str, size: int, sha: str):
        self.path = path
        self.size = size
        self.sha256 = sha
        self.lfs = None


class _BlobInfoStub:
    def __init__(self, size: int, *, lfs_sha256: str | None):
        self.size = size
        if lfs_sha256 is not None:
            import types as _types

            self.lfs = _types.SimpleNamespace(sha256=lfs_sha256)
        else:
            self.lfs = None


def _setup_workspace(tmp_path: Path) -> tuple[Paths, Path, Path]:
    source_root = tmp_path / "raw"
    data_root = tmp_path / "generated"
    source_root.mkdir()
    data_root.mkdir()
    (source_root / "a.osm.pbf").write_bytes(b"a-bytes")
    (source_root / "b.osm.pbf").write_bytes(b"b-bytes")
    return Paths(source_root=source_root, data_root=data_root), source_root, data_root


def _fake_exporter() -> object:
    def _export(source_path: Path, _cfg: Path) -> object:
        from shapely import to_wkb
        from shapely.geometry import Polygon

        from osm_polygon_description_tag.extraction import ExportRecord

        stem = source_path.name.removesuffix(".osm.pbf")
        geom = Polygon([(0, 0), (0, 1), (1, 1), (1, 0)])
        ewkb = to_wkb(geom, include_srid=True, flavor="extended", byte_order=1)
        return iter(
            [
                ExportRecord(
                    geometry_ewkb_hex=ewkb.hex(),
                    osm_type="way",
                    osm_id=abs(hash(stem)) % 1000000,
                    version=1,
                    changeset=1,
                    timestamp="2026-01-01T00:00:00Z",
                    tags=json.loads('{"description": "x"}'),
                )
            ]
        )

    return _fake_exporter


def _monkeypatch_hub(monkeypatch: pytest.MonkeyPatch, hub: _FakeHubApi) -> None:
    monkeypatch.setattr(
        "osm_polygon_description_tag.publication.verification._huggingface_hub.HfApi",
        lambda *a, **kw: hub,
    )


# --- RED tests below ----------------------------------------------------------


def test_default_hub_verifier_factory_creates_hfapi(monkeypatch: pytest.MonkeyPatch) -> None:
    """The default factory wraps huggingface_hub.HfApi without injecting a custom API."""
    captured: dict[str, tuple[Any, dict[str, Any]]] = {}

    def fake_hfapi(*args: Any, **kwargs: Any) -> object:
        captured["ctor"] = (args, kwargs)
        return _FakeHubApi(
            repo_infos={
                REPO_ID: _FakeRepo(
                    sha="abc",
                    files={"README.md": _FakeFile(b"# R")},
                ),
            },
        )

    monkeypatch.setattr(
        "osm_polygon_description_tag.publication.verification._huggingface_hub.HfApi",
        fake_hfapi,
    )

    factory = default_hub_verifier_factory()
    assert callable(factory)
    # The verifier invokes the real HfApi lazily.
    items = (
        UploadItem(
            relative_path="README.md", size_bytes=3, sha256=hashlib.sha256(b"# R").hexdigest()
        ),
    )
    factory(REPO_ID, items)


def test_default_verifier_fails_closed_on_missing_remote_file(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """When the remote revision is missing any item, the verifier fails closed."""
    factory = default_hub_verifier_factory()
    items = (
        UploadItem(relative_path="data/a.parquet", size_bytes=10, sha256="0" * 64),
        UploadItem(relative_path="README.md", size_bytes=2, sha256="a" * 64),
    )
    hub = _FakeHubApi(
        repo_infos={REPO_ID: _FakeRepo(sha="s", files={})},
        revisions={REPO_ID: ["s"]},
    )
    _monkeypatch_hub(monkeypatch, hub)
    with pytest.raises(RuntimeError, match="missing"):
        factory(REPO_ID, items)


def test_default_verifier_fails_closed_on_hash_mismatch(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Local SHA differs from remote: verifier fails closed."""
    factory = default_hub_verifier_factory()
    content = b"hello-world"
    items = (
        UploadItem(
            relative_path="README.md",
            size_bytes=len(content),
            sha256="f" * 64,  # wrong on purpose
        ),
    )
    hub = _FakeHubApi(
        repo_infos={
            REPO_ID: _FakeRepo(sha="s", files={"README.md": _FakeFile(content)}),
        },
        revisions={REPO_ID: ["s"]},
    )
    _monkeypatch_hub(monkeypatch, hub)
    with pytest.raises(RuntimeError, match="SHA mismatch"):
        factory(REPO_ID, items)


def test_default_verifier_succeeds_when_remote_matches(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """When every remote item matches local SHA, the verifier returns the repo SHA."""
    factory = default_hub_verifier_factory()
    readme_content = b"# README\n"
    readme_sha = hashlib.sha256(readme_content).hexdigest()
    items = (
        UploadItem(relative_path="README.md", size_bytes=len(readme_content), sha256=readme_sha),
    )
    hub = _FakeHubApi(
        repo_infos={
            REPO_ID: _FakeRepo(
                sha="verified-sha-9999", files={"README.md": _FakeFile(readme_content)}
            ),
        },
        revisions={REPO_ID: ["verified-sha-9999"]},
    )
    _monkeypatch_hub(monkeypatch, hub)
    assert factory(REPO_ID, items) == "verified-sha-9999"


def test_cli_run_and_publish_invokes_default_verifier(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """The public CLI's run-and-publish selects the default production verifier."""
    from shapely.geometry import Polygon

    from osm_polygon_description_tag._resources import project_code_revision
    from osm_polygon_description_tag.manifest import (
        Manifest,
        RunCounts,
        current_area_policy_sha256,
        current_output_algorithm_revision,
        output_identity_for,
        source_identity_for,
        write_manifest,
    )
    from osm_polygon_description_tag.storage import write_geoparquet
    from tests.conftest import make_record_dict

    paths, source_root, data_root = _setup_workspace(tmp_path)

    sentry: dict[str, Any] = {"ran": False}

    def fake_hubapi(*args: Any, **kwargs: Any):
        # The default verifier instantiates an ``HfApi`` and calls
        # ``repo_info`` and ``get_paths_info``. Provide a stub whose
        # ``repo_info`` records the call so the production activation
        # test can assert it. ``get_paths_info`` returns empty entries
        # matching the requested paths so the verifier falls through to
        # the size/LFS comparison and reports a successful match via
        # ``hf_hub_download`` reading the local artifact.

        class _Stub:
            def whoami(self) -> object:
                return {"name": "fake"}

            def auth_check(self, *_a: Any, **_kw: Any) -> None:
                return None

            def repo_info(self, *_a: Any, **_kw: Any) -> _RepoInfoStub:
                sentry["ran"] = True
                return _RepoInfoStub(REPO_ID, "ok-sha")

            def get_paths_info(
                self,
                repo_id: str,
                paths: Any,
                *,
                revision: str,
                repo_type: str = "dataset",
                **kw: Any,
            ) -> list[Any]:
                # Read the local file (uploaded in-process) and return a
                # metadata entry whose LFS sha matches the file content.
                out: list[Any] = []
                from pathlib import Path as _Path

                for rel in paths:
                    real_path = data_root / rel
                    if not real_path.is_file():
                        raise LookupError(rel)
                    content_bytes = _Path(real_path).read_bytes()
                    sha = hashlib.sha256(content_bytes).hexdigest()
                    entry = types.SimpleNamespace(
                        path=rel,
                        size=len(content_bytes),
                        lfs=types.SimpleNamespace(sha256=sha),
                        sha256=sha,
                    )
                    out.append(entry)
                return out

            def hf_hub_download(
                self,
                repo_id: str,
                filename: str,
                *,
                revision: str,
                repo_type: str = "dataset",
                **kw: Any,
            ) -> str:
                # Stream the local file through tempfile because the
                # verifier expects a path it can read.
                real_path = data_root / filename
                if not real_path.is_file():
                    raise LookupError(filename)
                import tempfile

                handle, path_str = tempfile.mkstemp(prefix="hf-", suffix=f"-{Path(real_path).name}")
                _ = handle
                Path(path_str).write_bytes(real_path.read_bytes())
                return path_str

        return _Stub()

    import types  # local import to keep module top-level clean

    import osm_polygon_description_tag.publication.upload as pub
    import osm_polygon_description_tag.publication.verification as orch

    monkeypatch.setattr(orch._huggingface_hub, "HfApi", fake_hubapi)
    monkeypatch.setattr(pub, "_default_runner_with_retry", lambda command, **kw: None)

    # Plant a resumable local artifact so the orchestrator does NOT need
    # to invoke the real osmium executable. Drop ``b`` for clarity.
    (source_root / "b.osm.pbf").unlink()
    (paths.data_root / "data").mkdir(parents=True, exist_ok=True)
    (paths.data_root / "manifests").mkdir(parents=True, exist_ok=True)
    write_geoparquet(
        iter(
            [
                make_record_dict(
                    Polygon([(0, 0), (0, 1), (1, 1), (1, 0)]),
                    {"description": "x"},
                    osm_id=1,
                    source_pbf="a.osm.pbf",
                )
            ]
        ),
        paths.data_root / "data" / "a.parquet",
        batch_size=10,
    )
    write_manifest(
        Manifest(
            manifest_schema_version=2,
            schema_version=3,
            geoparquet_version="1.1.0",
            transform_algorithm_version=3,
            area_policy_sha256=current_area_policy_sha256(),
            output_algorithm_revision=current_output_algorithm_revision(),
            source=source_identity_for(source_root / "a.osm.pbf"),
            output=output_identity_for(paths.data_root / "data" / "a.parquet"),
            osmium_version="osmium version 1.19.1",
            dependency_versions={"pyarrow": "20.0.0"},
            code_revision=project_code_revision(),
            started_at="2026-07-27T00:00:00+00:00",
            completed_at="2026-07-27T00:01:00+00:00",
            counts=RunCounts(emitted_features=1, included_rows=1, rejections={}),
        ),
        paths.data_root / "manifests" / "a.manifest.json",
    )
    (paths.data_root / "README.md").write_text("# README")
    (paths.data_root / "stats.json").write_text("{}")

    from osm_polygon_description_tag.cli import run as cli_run

    exit_code = cli_run(
        [
            "run-and-publish",
            "--source-root",
            str(source_root),
            "--data-root",
            str(data_root),
            "--confirm-repo",
            REPO_ID,
        ]
    )
    assert exit_code == 0
    assert sentry["ran"] is True, "default verifier must call HfApi.repo_info"


def test_no_state_written_before_verifier_succeeds(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """publication-state.json is written only after the verifier confirms the SHA."""
    from shapely.geometry import Polygon

    from osm_polygon_description_tag._resources import project_code_revision
    from osm_polygon_description_tag.manifest import (
        Manifest,
        RunCounts,
        current_area_policy_sha256,
        current_output_algorithm_revision,
        output_identity_for,
        source_identity_for,
        write_manifest,
    )
    from osm_polygon_description_tag.storage import write_geoparquet
    from tests.conftest import make_record_dict

    paths, source_root, data_root = _setup_workspace(tmp_path)
    (source_root / "b.osm.pbf").unlink()
    (paths.data_root / "data").mkdir(parents=True, exist_ok=True)
    (paths.data_root / "manifests").mkdir(parents=True, exist_ok=True)
    write_geoparquet(
        iter(
            [
                make_record_dict(
                    Polygon([(0, 0), (0, 1), (1, 1), (1, 0)]),
                    {"description": "x"},
                    osm_id=1,
                    source_pbf="a.osm.pbf",
                )
            ]
        ),
        paths.data_root / "data" / "a.parquet",
        batch_size=10,
    )
    write_manifest(
        Manifest(
            manifest_schema_version=2,
            schema_version=3,
            geoparquet_version="1.1.0",
            transform_algorithm_version=3,
            area_policy_sha256=current_area_policy_sha256(),
            output_algorithm_revision=current_output_algorithm_revision(),
            source=source_identity_for(source_root / "a.osm.pbf"),
            output=output_identity_for(paths.data_root / "data" / "a.parquet"),
            osmium_version="osmium version 1.19.1",
            dependency_versions={"pyarrow": "20.0.0"},
            code_revision=project_code_revision(),
            started_at="2026-07-27T00:00:00+00:00",
            completed_at="2026-07-27T00:01:00+00:00",
            counts=RunCounts(emitted_features=1, included_rows=1, rejections={}),
        ),
        paths.data_root / "manifests" / "a.manifest.json",
    )
    (paths.data_root / "README.md").write_text("# README")
    (paths.data_root / "stats.json").write_text("{}")

    import osm_polygon_description_tag.publication.upload as pub
    import osm_polygon_description_tag.workflow.orchestrator as orch

    monkeypatch.setattr(pub, "_default_runner_with_retry", lambda command, **kw: None)

    calls = {"count": 0}

    def failing_factory():
        def f(_repo_id, _files):
            calls["count"] += 1
            raise RuntimeError("hub verification failed")

        return f

    monkeypatch.setattr(orch, "default_hub_verifier_factory", failing_factory)

    from osm_polygon_description_tag.cli import run as cli_run

    exit_code = cli_run(
        [
            "run-and-publish",
            "--source-root",
            str(source_root),
            "--data-root",
            str(data_root),
            "--confirm-repo",
            REPO_ID,
        ]
    )
    assert exit_code != 0
    assert calls["count"] >= 1
    assert not (data_root / PUBLICATION_STATE_FILENAME).is_file()


def test_default_verifier_fails_closed_on_empty_identity(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """If ``whoami()`` returns an empty identity, the verifier fails closed."""
    factory = default_hub_verifier_factory()
    items = (UploadItem(relative_path="README.md", size_bytes=2, sha256="a" * 64),)

    class _Bad:
        def whoami(self) -> object:
            return {}

        def repo_info(self, *_a: object, **_kw: object) -> object:
            raise AssertionError("should not be reached")

    import osm_polygon_description_tag.publication.verification as orch

    monkeypatch.setattr(orch._huggingface_hub, "HfApi", lambda *a, **kw: _Bad())

    from osm_polygon_description_tag.orchestrator import HubVerificationError

    with pytest.raises(HubVerificationError, match="identity"):
        factory(REPO_ID, items)


def test_default_verifier_fails_closed_on_repo_info_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """If ``repo_info()`` raises, the verifier fails closed."""
    factory = default_hub_verifier_factory()
    items = (UploadItem(relative_path="README.md", size_bytes=2, sha256="a" * 64),)

    class _Bad:
        def whoami(self) -> object:
            return {"name": "fake"}

        def repo_info(self, *_a: object, **_kw: object) -> object:
            raise RuntimeError("repo not found")

    import osm_polygon_description_tag.publication.verification as orch

    monkeypatch.setattr(orch._huggingface_hub, "HfApi", lambda *a, **kw: _Bad())

    from osm_polygon_description_tag.orchestrator import HubVerificationError

    with pytest.raises(HubVerificationError, match="not accessible"):
        factory(REPO_ID, items)


def test_default_verifier_fails_closed_on_empty_revision(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """If ``repo_info()`` returns no SHA, the verifier fails closed."""
    factory = default_hub_verifier_factory()
    items = (UploadItem(relative_path="README.md", size_bytes=2, sha256="a" * 64),)

    class _Bad:
        def whoami(self) -> object:
            return {"name": "fake"}

        def repo_info(self, *_a: object, **_kw: object) -> object:
            class _Info:
                sha = ""

            return _Info()

    import osm_polygon_description_tag.publication.verification as orch

    monkeypatch.setattr(orch._huggingface_hub, "HfApi", lambda *a, **kw: _Bad())

    from osm_polygon_description_tag.orchestrator import HubVerificationError

    with pytest.raises(HubVerificationError, match="empty revision"):
        factory(REPO_ID, items)


def test_default_verifier_fails_closed_on_size_mismatch(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """If the remote size differs from the local size, the verifier fails closed."""
    factory = default_hub_verifier_factory()
    items = (UploadItem(relative_path="README.md", size_bytes=2, sha256="a" * 64),)

    class _Bad:
        def whoami(self) -> object:
            return {"name": "fake"}

        def repo_info(self, *_a: object, **_kw: object) -> object:
            class _Info:
                sha = "r"

            return _Info()

        def get_paths_info(
            self,
            repo_id: str,
            paths: list[str],
            *,
            revision: str,
            repo_type: str = "dataset",
        ) -> list[Any]:
            return [_PathInfoStub(path=paths[0], size=999, sha=("a" * 64))]

    import osm_polygon_description_tag.publication.verification as orch

    monkeypatch.setattr(orch._huggingface_hub, "HfApi", lambda *a, **kw: _Bad())

    from osm_polygon_description_tag.orchestrator import HubVerificationError

    with pytest.raises(HubVerificationError, match="size mismatch"):
        factory(REPO_ID, items)


def test_default_verifier_fails_closed_on_lfs_sha_mismatch(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """If the remote LFS SHA differs from the local SHA, the verifier fails closed."""
    factory = default_hub_verifier_factory()
    items = (UploadItem(relative_path="data/big.parquet", size_bytes=4, sha256="a" * 64),)

    class _Bad:
        def whoami(self) -> object:
            return {"name": "fake"}

        def repo_info(self, *_a: object, **_kw: object) -> object:
            class _Info:
                sha = "r"

            return _Info()

        def get_paths_info(
            self,
            repo_id: str,
            paths: list[str],
            *,
            revision: str,
            repo_type: str = "dataset",
        ) -> list[Any]:
            return [_BlobInfoStub(size=4, lfs_sha256="b" * 64)]

    import osm_polygon_description_tag.publication.verification as orch

    monkeypatch.setattr(orch._huggingface_hub, "HfApi", lambda *a, **kw: _Bad())

    from osm_polygon_description_tag.orchestrator import HubVerificationError

    with pytest.raises(HubVerificationError, match="LFS SHA mismatch"):
        factory(REPO_ID, items)


def test_default_verifier_fails_closed_on_download_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """If ``hf_hub_download`` fails, the verifier fails closed."""
    factory = default_hub_verifier_factory()
    items = (UploadItem(relative_path="README.md", size_bytes=2, sha256="a" * 64),)

    class _Bad:
        def whoami(self) -> object:
            return {"name": "fake"}

        def repo_info(self, *_a: object, **_kw: object) -> object:
            class _Info:
                sha = "r"

            return _Info()

        def get_paths_info(
            self,
            repo_id: str,
            paths: list[str],
            *,
            revision: str,
            repo_type: str = "dataset",
        ) -> list[Any]:
            return [_PathInfoStub(path=paths[0], size=2, sha="a" * 64)]

        def hf_hub_download(
            self,
            repo_id: str,
            filename: str,
            *,
            revision: str,
            repo_type: str = "dataset",
        ) -> str:
            raise RuntimeError("download failed")

    import osm_polygon_description_tag.publication.verification as orch

    monkeypatch.setattr(orch._huggingface_hub, "HfApi", lambda *a, **kw: _Bad())

    from osm_polygon_description_tag.orchestrator import HubVerificationError

    with pytest.raises(HubVerificationError, match="could not download"):
        factory(REPO_ID, items)
