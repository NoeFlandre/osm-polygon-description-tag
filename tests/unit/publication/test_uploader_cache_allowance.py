"""RED tests proving the uploader-owned ``.cache/huggingface`` directory is
allowed locally but never included in any UploadPlan or upload command.

Production reality: the installed ``hf upload-large-folder`` implementation
creates ``<data-root>/.cache/huggingface/upload/...metadata`` while it
runs. The current allowlist rejects any top-level entry that is not
explicitly listed, so the second invocation (or the final metadata
upload) fails before it can run.

The amendments:

- The exact uploader-owned ``.cache/huggingface`` directory is permitted
  locally.
- It is never included in any UploadPlan or upload command.
- ``.cache`` must be a real directory, not a symlink.
- Unrelated top-level hidden files/directories remain rejected.
- The cache survives interruption and restart (no cleanup deletes it).
- The cache cannot be added to ``_ALLOWED_TOP_LEVEL`` as a plain top-level
  entry because it must NEVER appear in the upload plan.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from osm_polygon_description_tag.publication import (
    REPO_ID,
    PublicationError,
    _build_metadata_only_upload_plan,
    _build_per_pbf_upload_plan,
    _collect_allowlisted_files,
    create_upload_plan,
    metadata_only_command,
    per_pbf_command,
)


def _setup_data_root(tmp_path: Path) -> Path:
    """Return a data root that has the minimum required publication files."""
    data_root = tmp_path / "generated"
    data_root.mkdir()
    (data_root / "README.md").write_text("# README")
    (data_root / "stats.json").write_text("{}")
    (data_root / "data").mkdir()
    (data_root / "manifests").mkdir()
    return data_root


def _setup_source_root(tmp_path: Path) -> Path:
    source_root = tmp_path / "raw"
    source_root.mkdir()
    (source_root / "a.osm.pbf").write_bytes(b"a-bytes")
    return source_root


def _write_resumable_artifact(data_root: Path, source_name: str) -> None:
    """Plant a complete Parquet + manifest so the per-PBF plan can be built."""
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

    source_root = data_root.parent / "raw"
    source_path = source_root / source_name
    if not source_path.exists():
        source_root.mkdir(parents=True, exist_ok=True)
        source_path.write_bytes(b"fake pbf")

    stem = source_name.removesuffix(".osm.pbf")
    write_geoparquet(
        iter(
            [
                make_record_dict(
                    Polygon([(0, 0), (0, 1), (1, 1), (1, 0)]),
                    {"description": "x"},
                    osm_id=1,
                    source_pbf=source_name,
                )
            ]
        ),
        data_root / "data" / f"{stem}.parquet",
        batch_size=10,
    )
    write_manifest(
        Manifest(
            manifest_schema_version=2,
            schema_version=2,
            geoparquet_version="1.1.0",
            transform_algorithm_version=2,
            area_policy_sha256=current_area_policy_sha256(),
            output_algorithm_revision=current_output_algorithm_revision(),
            source=source_identity_for(source_path),
            output=output_identity_for(data_root / "data" / f"{stem}.parquet"),
            osmium_version="osmium version 1.19.1",
            dependency_versions={"pyarrow": "20.0.0"},
            code_revision=project_code_revision(),
            started_at="2026-07-27T00:00:00+00:00",
            completed_at="2026-07-27T00:01:00+00:00",
            counts=RunCounts(emitted_features=1, included_rows=1, rejections={}),
        ),
        data_root / "manifests" / f"{stem}.manifest.json",
    )


def test_uploader_cache_directory_is_accepted_in_collect(tmp_path: Path) -> None:
    """The exact uploader-owned ``.cache/huggingface`` directory is permitted."""
    data_root = _setup_data_root(tmp_path)
    cache = data_root / ".cache" / "huggingface"
    cache.mkdir(parents=True)
    (cache / "upload").mkdir()
    (cache / "upload" / "metadata.bin").write_bytes(b"meta")

    # Must not raise: the cache is allowed locally.
    items = _collect_allowlisted_files(data_root)
    relative_paths = sorted(item.relative_path for item in items)
    assert "README.md" in relative_paths
    assert "stats.json" in relative_paths
    # The cache itself is NOT in the plan.
    assert not any(item.relative_path.startswith(".cache") for item in items)


def test_macos_ds_store_is_ignored_locally_and_never_uploaded(tmp_path: Path) -> None:
    """Finder metadata must not abort or enter a dataset upload plan."""
    data_root = _setup_data_root(tmp_path)
    (data_root / ".DS_Store").write_bytes(b"Finder metadata")

    items = _collect_allowlisted_files(data_root)

    assert {item.relative_path for item in items} == {"README.md", "stats.json"}


def test_macos_ds_store_symlink_is_rejected(tmp_path: Path) -> None:
    """The exception is limited to a regular local file."""
    data_root = _setup_data_root(tmp_path)
    outside = tmp_path / "outside"
    outside.write_bytes(b"outside")
    try:
        (data_root / ".DS_Store").symlink_to(outside)
    except OSError:
        pytest.skip("filesystem does not support symlinks")

    with pytest.raises(PublicationError, match="regular file"):
        _collect_allowlisted_files(data_root)


def test_uploader_cache_never_in_per_pbf_plan(tmp_path: Path) -> None:
    """The per-PBF plan never contains anything from ``.cache/huggingface``."""
    data_root = _setup_data_root(tmp_path)
    (data_root / ".cache" / "huggingface").mkdir(parents=True)
    _write_resumable_artifact(data_root, "a.osm.pbf")

    plan = _build_per_pbf_upload_plan(data_root, "a.osm.pbf")
    relative = sorted(item.relative_path for item in plan.files)
    assert relative == sorted(
        [
            "README.md",
            "data/a.parquet",
            "manifests/a.manifest.json",
            "stats.json",
        ]
    )
    assert not any(item.relative_path.startswith(".cache") for item in plan.files)


def test_uploader_cache_never_in_metadata_only_plan(tmp_path: Path) -> None:
    """The metadata-only plan never contains anything from ``.cache/huggingface``."""
    data_root = _setup_data_root(tmp_path)
    (data_root / ".cache" / "huggingface").mkdir(parents=True)

    plan = _build_metadata_only_upload_plan(data_root)
    relative = sorted(item.relative_path for item in plan.files)
    assert relative == sorted(["README.md", "stats.json"])


def test_uploader_cache_never_in_upload_command(tmp_path: Path) -> None:
    """The per-PBF upload command's ``--include`` flags never reference the cache."""
    data_root = _setup_data_root(tmp_path)
    (data_root / ".cache" / "huggingface").mkdir(parents=True)
    _write_resumable_artifact(data_root, "a.osm.pbf")

    command = per_pbf_command(data_root, "a.osm.pbf")
    assert ".cache" not in command, f"cache must not appear in command: {command}"

    command = metadata_only_command(data_root)
    assert ".cache" not in command, f"cache must not appear in command: {command}"


def test_data_root_create_upload_plan_accepts_cache(tmp_path: Path) -> None:
    """The dataset-wide ``create_upload_plan`` accepts the cache directory."""
    data_root = _setup_data_root(tmp_path)
    (data_root / ".cache" / "huggingface" / "upload").mkdir(parents=True)
    (data_root / ".cache" / "huggingface" / "upload" / "x.bin").write_bytes(b"x")

    plan = create_upload_plan(data_root)
    assert not any(item.relative_path.startswith(".cache") for item in plan.files)


def test_cache_symlink_is_rejected(tmp_path: Path) -> None:
    """A ``.cache`` symlink is rejected: cache must be a real directory."""
    data_root = _setup_data_root(tmp_path)
    real_dir = tmp_path / "real"
    real_dir.mkdir()
    cache_link = data_root / ".cache"
    try:
        cache_link.symlink_to(real_dir)
    except OSError:
        pytest.skip("filesystem does not support symlinks")
    with pytest.raises(PublicationError):
        _collect_allowlisted_files(data_root)


def test_unrelated_hidden_files_remain_rejected(tmp_path: Path) -> None:
    """Top-level hidden files/directories other than ``.cache/huggingface`` are rejected."""
    data_root = _setup_data_root(tmp_path)
    # An unrelated hidden directory must be rejected.
    (data_root / ".hidden_dir").mkdir()
    with pytest.raises(PublicationError):
        _collect_allowlisted_files(data_root)
    # An unrelated hidden file must be rejected.
    (data_root / ".env").write_text("x")
    with pytest.raises(PublicationError):
        _collect_allowlisted_files(data_root)


def test_uploader_cache_survives_across_runs(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The cache directory survives interruption and restart; no cleanup deletes it."""
    from shapely.geometry import Polygon

    from osm_polygon_description_tag._resources import project_code_revision
    from osm_polygon_description_tag.cli import run as cli_run
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

    data_root = tmp_path / "generated"
    source_root = tmp_path / "raw"
    source_root.mkdir()
    data_root.mkdir()
    (source_root / "a.osm.pbf").write_bytes(b"a-bytes")
    (data_root / "README.md").write_text("# README")
    (data_root / "stats.json").write_text("{}")
    (data_root / "data").mkdir()
    (data_root / "manifests").mkdir()

    # Plant a resumable artifact so the publish path only does upload/verify.
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
        data_root / "data" / "a.parquet",
        batch_size=10,
    )
    write_manifest(
        Manifest(
            manifest_schema_version=2,
            schema_version=2,
            geoparquet_version="1.1.0",
            transform_algorithm_version=2,
            area_policy_sha256=current_area_policy_sha256(),
            output_algorithm_revision=current_output_algorithm_revision(),
            source=source_identity_for(source_root / "a.osm.pbf"),
            output=output_identity_for(data_root / "data" / "a.parquet"),
            osmium_version="osmium version 1.19.1",
            dependency_versions={"pyarrow": "20.0.0"},
            code_revision=project_code_revision(),
            started_at="2026-07-27T00:00:00+00:00",
            completed_at="2026-07-27T00:01:00+00:00",
            counts=RunCounts(emitted_features=1, included_rows=1, rejections={}),
        ),
        data_root / "manifests" / "a.manifest.json",
    )

    cache_dir = data_root / ".cache" / "huggingface" / "upload"
    cache_dir.mkdir(parents=True)
    cache_file = cache_dir / "metadata.bin"
    cache_file.write_bytes(b"in-flight state")

    # First upload: runner simulates the real uploader creating the cache
    # directory, then completes successfully.
    def fake_runner(command: list[str], timeout: float | None = None) -> str:
        # Simulate the real ``hf upload-large-folder`` writing the cache.
        cache_dir.mkdir(parents=True, exist_ok=True)
        cache_file.write_bytes(b"updated-by-runner")
        return "rev-1"

    def stub_hf_api_factory():
        def f(repo_id: str, files) -> str:
            return "rev-1"

        return f

    import osm_polygon_description_tag.publication.upload as pub
    import osm_polygon_description_tag.workflow.orchestrator as orch
    import osm_polygon_description_tag.workflow.preflight as preflight_module

    monkeypatch.setattr(pub, "_default_runner_with_retry", fake_runner)
    monkeypatch.setattr(orch, "default_hub_verifier_factory", stub_hf_api_factory)
    monkeypatch.setattr(orch, "_default_clock", lambda: "2026-07-27T00:00:00+00:00")

    # Patch HfApi to avoid live auth.
    class _Stub:
        def whoami(self) -> object:
            return {"name": "fake"}

        def repo_info(self, *_a: object, **_kw: object) -> object:
            class _Info:
                sha = "abc"

            return _Info()

        def auth_check(self, *_a: object, **_kw: object) -> None:
            return None

    monkeypatch.setattr(preflight_module._huggingface_hub, "HfApi", lambda *_a, **_kw: _Stub())

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
    # The runner created the cache; the cache must still be present.
    assert cache_dir.is_dir(), "cache was deleted by the run"
    assert cache_file.read_bytes() == b"updated-by-runner"

    # Second upload: run again, demonstrate cache survives intact.
    def fake_runner_repeat(command: list[str], timeout: float | None = None) -> str:
        # The cache must still be present (not deleted by the previous run).
        assert cache_file.read_bytes() == b"updated-by-runner"
        return "rev-2"

    monkeypatch.setattr(pub, "_default_runner_with_retry", fake_runner_repeat)

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
    # Cache still present after both runs.
    assert cache_dir.is_dir()
    assert cache_file.read_bytes() == b"updated-by-runner"
