"""Adverse filesystem and payload cases for the public Grid staging API."""

import hashlib
import json
import os
from pathlib import Path
from typing import Any

import pytest
from shapely.geometry import Polygon

from osm_polygon_description_tag.dataset.languages.checkpoint import (
    ShardCheckpoint,
    ShardStatus,
    shard_paths,
    write_checkpoint,
)
from osm_polygon_description_tag.dataset.languages.snapshot import (
    SNAPSHOT_FILENAME,
    SnapshotManifest,
    fingerprint_lockfile,
    fingerprint_project_source,
    prepare_snapshot,
)
from osm_polygon_description_tag.storage import write_geoparquet
from osm_polygon_description_tag.workflow import grid_operator
from osm_polygon_description_tag.workflow.grid_operator import (
    JOB_CONFIG_FILENAME,
    MAX_PROCESSING_SECONDS,
    MAX_WALLTIME_SECONDS,
    REQUIRED_CORES,
    GridOperatorError,
    PreparedJob,
    build_bundle_transfer_argv,
    build_result_retrieval_argv,
    bundle_for_shard,
    job_paths,
    prepare_portable_job,
    verify_prepared_bundle,
)
from tests.conftest import make_record_dict
from tests.helpers.messages import exactly
from tests.helpers.sentences import REMOTE_SAT_MODEL_PATH

SHARD = "region.parquet"
REMOTE_BUNDLE = "/scratch/lang-bundle"


@pytest.fixture
def portable_inputs(tmp_path: Path) -> tuple[Path, Path, Path, SnapshotManifest]:
    source = tmp_path / "source"
    source.mkdir()
    write_geoparquet(
        iter(
            [
                make_record_dict(
                    Polygon([(0, 0), (0, 1), (1, 1), (1, 0)]),
                    {"description": "A portable description"},
                )
            ]
        ),
        source / SHARD,
        batch_size=1,
    )

    project = tmp_path / "project"
    (project / "src").mkdir(parents=True)
    (project / "src" / "module.py").write_text("VALUE = 1\n", encoding="utf-8")
    (project / "pyproject.toml").write_text("[project]\nname = 'synthetic'\n", encoding="utf-8")
    (project / "uv.lock").write_text("version = 1\n", encoding="utf-8")

    run = tmp_path / "run"
    snapshot = prepare_snapshot(
        source,
        run,
        code_fingerprint=fingerprint_project_source(project),
        lock_fingerprint=fingerprint_lockfile(project),
    )
    return project, source, run, snapshot


def _stage(
    inputs: tuple[Path, Path, Path, SnapshotManifest], *, remote_bundle_dir: str = REMOTE_BUNDLE
) -> PreparedJob:
    project, source, run, snapshot = inputs
    return prepare_portable_job(
        run,
        project,
        source,
        snapshot,
        SHARD,
        remote_bundle_dir=remote_bundle_dir,
        sat_model_path=REMOTE_SAT_MODEL_PATH,
    )


def _rewrite_json(path: Path, mutate: Any) -> None:
    payload = json.loads(path.read_text(encoding="utf-8"))
    mutate(payload)
    path.write_text(json.dumps(payload), encoding="utf-8")


def _refresh_stage_descriptor(stage_path: Path, relative_path: str, path: Path) -> None:
    digest = hashlib.sha256(path.read_bytes()).hexdigest()

    def update(payload: dict[str, Any]) -> None:
        descriptor = next(
            item for item in payload["files"] if item["relative_path"] == relative_path
        )
        descriptor["size_bytes"] = path.stat().st_size
        descriptor["sha256"] = digest

    _rewrite_json(stage_path, update)


def _write_paused_checkpoint(
    run: Path, snapshot: SnapshotManifest, *, snapshot_id: str | None = None
) -> None:
    state = shard_paths(run, SHARD)
    write_checkpoint(
        state.checkpoint,
        ShardCheckpoint(
            snapshot_id=snapshot_id or snapshot.snapshot_id,
            model_config_fingerprint=snapshot.model_config_fingerprint,
            shard=SHARD,
            batch_size=1,
            input_row_count=snapshot.source_file(SHARD).row_count,
            input_cursor=0,
            annotation_count=0,
            completed_parts=(),
            status=ShardStatus.PAUSED,
        ),
    )


@pytest.mark.parametrize(
    ("overrides", "message"),
    [
        ({"remote_bundle_dir": "relative"}, "absolute path"),
        ({"remote_bundle_dir": "/scratch/a b"}, "shell metacharacters"),
        ({"remote_bundle_dir": ""}, "non-empty string"),
        ({"processing_seconds": 0}, "processing budget must be between"),
        ({"batch_size": 0}, "batch size must be a positive integer"),
        ({"walltime_seconds": MAX_WALLTIME_SECONDS + 1}, "walltime must be between"),
    ],
)
def test_prepare_portable_job_rejects_invalid_remote_and_config_inputs(
    portable_inputs: tuple[Path, Path, Path, SnapshotManifest],
    overrides: dict[str, object],
    message: str,
) -> None:
    kwargs: dict[str, object] = {"remote_bundle_dir": REMOTE_BUNDLE, **overrides}
    project, source, run, snapshot = portable_inputs

    with pytest.raises(GridOperatorError, match=message):
        prepare_portable_job(
            run, project, source, snapshot, SHARD, **kwargs, sat_model_path=REMOTE_SAT_MODEL_PATH
        )  # type: ignore[arg-type]


@pytest.mark.parametrize(
    ("drift", "message"),
    [
        ("source", f"source file does not match snapshot: {SHARD}"),
        ("project", "project source does not match the bundle code fingerprint"),
        ("lock", "project lockfile does not match the bundle lock fingerprint"),
    ],
)
def test_prepare_portable_job_revalidates_source_and_project_identity(
    portable_inputs: tuple[Path, Path, Path, SnapshotManifest], drift: str, message: str
) -> None:
    project, source, run, snapshot = portable_inputs
    if drift == "source":
        write_geoparquet(
            iter(
                [
                    make_record_dict(
                        Polygon([(0, 0), (0, 1), (1, 1), (1, 0)]),
                        {"description": "changed"},
                    )
                ]
            ),
            source / SHARD,
            batch_size=1,
        )
    elif drift == "project":
        (project / "src" / "module.py").write_text("VALUE = 2\n", encoding="utf-8")
    else:
        (project / "uv.lock").write_text("version = 2\n", encoding="utf-8")

    with pytest.raises(GridOperatorError, match=exactly(message)):
        _stage(portable_inputs)


def test_prepare_portable_job_rejects_a_foreign_run_snapshot(
    portable_inputs: tuple[Path, Path, Path, SnapshotManifest], tmp_path: Path
) -> None:
    project, source, run, _ = portable_inputs
    foreign = prepare_snapshot(
        source,
        tmp_path / "foreign-run",
        code_fingerprint="c" * 64,
        lock_fingerprint=fingerprint_lockfile(project),
    )

    with pytest.raises(
        GridOperatorError,
        match=exactly("run snapshot does not match the requested portable bundle"),
    ):
        _stage((project, source, run, foreign))


def test_prepare_portable_job_rejects_an_invalid_project_file(
    portable_inputs: tuple[Path, Path, Path, SnapshotManifest],
) -> None:
    project, _, _, _ = portable_inputs
    (project / "pyproject.toml").unlink()
    (project / "pyproject.toml").mkdir()

    with pytest.raises(GridOperatorError, match="project staging input is missing"):
        _stage(portable_inputs)


def test_prepare_portable_job_rejects_a_readme_symlink(
    portable_inputs: tuple[Path, Path, Path, SnapshotManifest], tmp_path: Path
) -> None:
    project, _, _, _ = portable_inputs
    target = tmp_path / "external-readme.md"
    target.write_text("external\n", encoding="utf-8")
    (project / "README.md").symlink_to(target)

    with pytest.raises(GridOperatorError, match="project staging input is not a regular file"):
        _stage(portable_inputs)


def test_prepare_portable_job_ignores_project_cache_files_and_directories(
    portable_inputs: tuple[Path, Path, Path, SnapshotManifest],
) -> None:
    project, _, _, _ = portable_inputs
    cache = project / "src" / "__pycache__"
    cache.mkdir()
    (cache / "ignored.py").write_text("cached\n", encoding="utf-8")
    (project / "src" / "ignored.pyc").write_bytes(b"cached")
    (project / "src" / "empty-directory").mkdir()

    prepared = _stage(portable_inputs)

    assert (prepared.project_root / "src" / "module.py").is_file()
    assert not (prepared.project_root / "src" / "__pycache__").exists()
    assert not (prepared.project_root / "src" / "ignored.pyc").exists()
    assert not (prepared.project_root / "src" / "empty-directory").exists()


def test_prepare_portable_job_rejects_a_non_file_project_source_entry(
    portable_inputs: tuple[Path, Path, Path, SnapshotManifest],
) -> None:
    if not hasattr(os, "mkfifo"):
        pytest.skip("the platform does not provide named pipes")
    project, _, _, _ = portable_inputs
    fifo = project / "src" / "not-a-file"
    os.mkfifo(fifo)

    with pytest.raises(GridOperatorError, match="project source contains a non-file"):
        _stage(portable_inputs)


@pytest.mark.parametrize(
    ("shape", "message"),
    [
        ("symlink", "resume checkpoint must not be a symlink"),
        ("directory", "resume checkpoint must be a regular file"),
        ("orphan", "resume artifacts exist without a checkpoint"),
    ],
)
def test_prepare_portable_job_rejects_invalid_resume_checkpoint_shapes(
    portable_inputs: tuple[Path, Path, Path, SnapshotManifest],
    tmp_path: Path,
    shape: str,
    message: str,
) -> None:
    _, _, run, _ = portable_inputs
    state = shard_paths(run, SHARD)
    state.root.mkdir(parents=True)
    if shape == "symlink":
        state.checkpoint.symlink_to(tmp_path / "external-checkpoint.json")
    elif shape == "directory":
        state.checkpoint.mkdir()
    else:
        state.parts.mkdir()

    with pytest.raises(GridOperatorError, match=exactly(message)):
        _stage(portable_inputs)


@pytest.mark.parametrize("shape", ["symlink", "file"])
def test_prepare_portable_job_rejects_invalid_resume_state_roots(
    portable_inputs: tuple[Path, Path, Path, SnapshotManifest],
    tmp_path: Path,
    shape: str,
) -> None:
    _, _, run, _ = portable_inputs
    state = shard_paths(run, SHARD)
    state.root.parent.mkdir(parents=True)
    if shape == "symlink":
        target = tmp_path / "external-state"
        target.mkdir()
        state.root.symlink_to(target, target_is_directory=True)
    else:
        state.root.write_text("not a directory", encoding="utf-8")

    with pytest.raises(GridOperatorError, match="resume state"):
        _stage(portable_inputs)


def test_prepare_portable_job_rejects_resume_state_with_unexpected_run_artifacts(
    portable_inputs: tuple[Path, Path, Path, SnapshotManifest],
) -> None:
    project, _, run, snapshot = portable_inputs
    _write_paused_checkpoint(run, snapshot)
    (run / "shards" / "unexpected-shard").mkdir()

    with pytest.raises(
        GridOperatorError, match=exactly("resume state failed collection validation")
    ):
        prepare_portable_job(
            run,
            project,
            portable_inputs[1],
            snapshot,
            SHARD,
            remote_bundle_dir=REMOTE_BUNDLE,
            sat_model_path=REMOTE_SAT_MODEL_PATH,
        )


def test_prepare_portable_job_rejects_resume_checkpoint_with_foreign_identity(
    portable_inputs: tuple[Path, Path, Path, SnapshotManifest],
) -> None:
    project, _, run, snapshot = portable_inputs
    _write_paused_checkpoint(run, snapshot, snapshot_id="c" * 64)

    with pytest.raises(
        GridOperatorError,
        match=exactly("existing shard checkpoint is not bound to this job bundle"),
    ):
        prepare_portable_job(
            run,
            project,
            portable_inputs[1],
            snapshot,
            SHARD,
            remote_bundle_dir=REMOTE_BUNDLE,
            sat_model_path=REMOTE_SAT_MODEL_PATH,
        )


def test_prepare_portable_job_stages_a_valid_paused_checkpoint(
    portable_inputs: tuple[Path, Path, Path, SnapshotManifest],
) -> None:
    project, source, run, snapshot = portable_inputs
    _write_paused_checkpoint(run, snapshot)

    prepared = prepare_portable_job(
        run,
        project,
        source,
        snapshot,
        SHARD,
        remote_bundle_dir=REMOTE_BUNDLE,
        sat_model_path=REMOTE_SAT_MODEL_PATH,
    )

    staged_checkpoint = shard_paths(prepared.run_root, SHARD).checkpoint
    assert staged_checkpoint.is_file()
    assert verify_prepared_bundle(prepared.payload_root) == prepared.bundle


def test_verify_prepared_bundle_rejects_a_canonical_foreign_staged_snapshot(
    portable_inputs: tuple[Path, Path, Path, SnapshotManifest], tmp_path: Path
) -> None:
    prepared = _stage(portable_inputs)
    project, source, _, snapshot = portable_inputs
    foreign = prepare_snapshot(
        source,
        tmp_path / "foreign-run",
        code_fingerprint="c" * 64,
        lock_fingerprint=snapshot.lock_fingerprint,
    )
    staged_snapshot = prepared.run_root / SNAPSHOT_FILENAME
    staged_snapshot.write_text(foreign.to_json(), encoding="utf-8")
    _refresh_stage_descriptor(prepared.manifest, f"run/{SNAPSHOT_FILENAME}", staged_snapshot)

    with pytest.raises(
        GridOperatorError, match=exactly("staged snapshot id does not match bundle")
    ):
        verify_prepared_bundle(prepared.payload_root)


def test_prepare_portable_job_cleans_temporary_payload_after_copy_failure(
    portable_inputs: tuple[Path, Path, Path, SnapshotManifest], monkeypatch: pytest.MonkeyPatch
) -> None:
    project, source, run, snapshot = portable_inputs

    def fail_copy(source_path: Path, destination: Path) -> None:
        raise OSError("simulated copy failure")

    monkeypatch.setattr(grid_operator.shutil, "copyfile", fail_copy)
    bundle = bundle_for_shard(snapshot, SHARD)
    paths = job_paths(run, bundle)
    with pytest.raises(GridOperatorError, match="cannot stage"):
        prepare_portable_job(
            run,
            project,
            source,
            snapshot,
            SHARD,
            remote_bundle_dir=REMOTE_BUNDLE,
            sat_model_path=REMOTE_SAT_MODEL_PATH,
        )

    assert not tuple(paths.root.glob(".payload-*"))


def test_prepare_portable_job_cleans_temporary_payload_after_rename_failure(
    portable_inputs: tuple[Path, Path, Path, SnapshotManifest], monkeypatch: pytest.MonkeyPatch
) -> None:
    project, source, run, snapshot = portable_inputs
    bundle = bundle_for_shard(snapshot, SHARD)
    paths = job_paths(run, bundle)
    original_replace = grid_operator.os.replace

    def fail_payload_replace(source_path: object, destination: object) -> None:
        destination_path = Path(destination)
        if destination_path.parent == paths.root and destination_path.name.startswith("payload-"):
            raise OSError("simulated payload rename failure")
        original_replace(source_path, destination)

    monkeypatch.setattr(grid_operator.os, "replace", fail_payload_replace)
    with pytest.raises(OSError, match="payload rename failure"):
        prepare_portable_job(
            run,
            project,
            source,
            snapshot,
            SHARD,
            remote_bundle_dir=REMOTE_BUNDLE,
            sat_model_path=REMOTE_SAT_MODEL_PATH,
        )

    assert not tuple(paths.root.glob(".payload-*"))


def test_prepare_portable_job_rejects_a_symlinked_payload_root(
    portable_inputs: tuple[Path, Path, Path, SnapshotManifest],
) -> None:
    prepared = _stage(portable_inputs)
    saved = prepared.payload_root.with_name("saved-payload")
    prepared.payload_root.rename(saved)
    prepared.payload_root.symlink_to(saved, target_is_directory=True)

    with pytest.raises(GridOperatorError, match="portable payload must not be a symlink"):
        _stage(portable_inputs)


def test_prepare_portable_job_rejects_tampered_reusable_payload(
    portable_inputs: tuple[Path, Path, Path, SnapshotManifest],
) -> None:
    prepared = _stage(portable_inputs)
    (prepared.project_root / "src" / "module.py").write_text("VALUE = tampered\n", encoding="utf-8")

    with pytest.raises(GridOperatorError, match="staged file hash does not match"):
        _stage(portable_inputs)


def test_verify_prepared_bundle_rejects_a_payload_root_symlink(
    portable_inputs: tuple[Path, Path, Path, SnapshotManifest],
) -> None:
    prepared = _stage(portable_inputs)
    saved = prepared.payload_root.with_name("saved-payload")
    prepared.payload_root.rename(saved)
    prepared.payload_root.symlink_to(saved, target_is_directory=True)

    with pytest.raises(GridOperatorError, match="portable payload root is not a regular directory"):
        verify_prepared_bundle(prepared.payload_root)


def test_verify_prepared_bundle_rejects_a_payload_symlink(
    portable_inputs: tuple[Path, Path, Path, SnapshotManifest], tmp_path: Path
) -> None:
    prepared = _stage(portable_inputs)
    target = tmp_path / "external.py"
    target.write_text("external\n", encoding="utf-8")
    (prepared.project_root / "src" / "linked.py").symlink_to(target)

    with pytest.raises(GridOperatorError, match="portable payload contains a symlink"):
        verify_prepared_bundle(prepared.payload_root)


def test_verify_prepared_bundle_rejects_missing_and_malformed_stage_manifests(
    portable_inputs: tuple[Path, Path, Path, SnapshotManifest],
) -> None:
    prepared = _stage(portable_inputs)
    prepared.manifest.unlink()
    with pytest.raises(
        GridOperatorError, match=exactly("portable payload is missing its stage manifest")
    ):
        verify_prepared_bundle(prepared.payload_root)

    prepared.manifest.write_text("{not-json", encoding="utf-8")
    with pytest.raises(GridOperatorError, match="cannot read stage manifest"):
        verify_prepared_bundle(prepared.payload_root)


@pytest.mark.parametrize(
    ("field", "value", "message"),
    [
        ("stage_schema_version", 2, "unsupported stage schema version"),
        ("bundle_id", "c" * 64, "stage manifest bundle id does not match bundle"),
        (
            "resume_state_fingerprint",
            "d" * 64,
            "stage manifest resume state fingerprint does not match payload",
        ),
    ],
)
def test_verify_prepared_bundle_rejects_tampered_stage_metadata(
    portable_inputs: tuple[Path, Path, Path, SnapshotManifest],
    field: str,
    value: object,
    message: str,
) -> None:
    prepared = _stage(portable_inputs)
    _rewrite_json(prepared.manifest, lambda payload: payload.__setitem__(field, value))

    with pytest.raises(GridOperatorError, match=exactly(message)):
        verify_prepared_bundle(prepared.payload_root)


@pytest.mark.parametrize(
    ("field", "value", "message"),
    [
        ("job_config_schema_version", 2, "unsupported job config schema version"),
        ("bundle_id", "c" * 64, "job config bundle id does not match the prepared bundle"),
        ("cores", 2, f"job config must request exactly {REQUIRED_CORES} core"),
        ("thread_limit", 2, "job config thread limit must be 1"),
        (
            "processing_seconds",
            0,
            f"processing budget must be between 1 and {MAX_PROCESSING_SECONDS} seconds",
        ),
    ],
)
def test_verify_prepared_bundle_rejects_tampered_job_config(
    portable_inputs: tuple[Path, Path, Path, SnapshotManifest],
    field: str,
    value: object,
    message: str,
) -> None:
    prepared = _stage(portable_inputs)
    config = prepared.payload_root / JOB_CONFIG_FILENAME
    _rewrite_json(config, lambda payload: payload.__setitem__(field, value))
    _refresh_stage_descriptor(prepared.manifest, JOB_CONFIG_FILENAME, config)

    with pytest.raises(GridOperatorError, match=exactly(message)):
        verify_prepared_bundle(prepared.payload_root)


def test_verify_prepared_bundle_rejects_an_unlisted_required_file(
    portable_inputs: tuple[Path, Path, Path, SnapshotManifest],
) -> None:
    prepared = _stage(portable_inputs)

    def remove_lock(payload: dict[str, Any]) -> None:
        payload["files"] = [
            item for item in payload["files"] if item["relative_path"] != "project/uv.lock"
        ]

    (prepared.project_root / "uv.lock").unlink()
    _rewrite_json(prepared.manifest, remove_lock)

    with pytest.raises(GridOperatorError, match="missing required files"):
        verify_prepared_bundle(prepared.payload_root)


def test_verify_prepared_bundle_rejects_a_foreign_expected_bundle(
    portable_inputs: tuple[Path, Path, Path, SnapshotManifest], tmp_path: Path
) -> None:
    prepared = _stage(portable_inputs)
    project, source, _, _ = portable_inputs
    foreign_snapshot = prepare_snapshot(
        source,
        tmp_path / "foreign-run",
        code_fingerprint="c" * 64,
        lock_fingerprint=fingerprint_lockfile(project),
    )

    with pytest.raises(
        GridOperatorError, match=exactly("staged bundle does not match the expected bundle")
    ):
        verify_prepared_bundle(
            prepared.payload_root, expected_bundle=bundle_for_shard(foreign_snapshot, SHARD)
        )


def test_bundle_transfer_requires_a_verified_prepared_job(
    portable_inputs: tuple[Path, Path, Path, SnapshotManifest],
) -> None:
    with pytest.raises(GridOperatorError, match=exactly("prepared bundle must be a PreparedJob")):
        build_bundle_transfer_argv(object(), REMOTE_BUNDLE)  # type: ignore[arg-type]

    prepared = _stage(portable_inputs)
    prepared_config = prepared.payload_root / JOB_CONFIG_FILENAME
    prepared_config.write_text("tampered", encoding="utf-8")
    with pytest.raises(GridOperatorError, match="staged file hash does not match"):
        build_bundle_transfer_argv(prepared, REMOTE_BUNDLE)


@pytest.mark.parametrize("remote", ["relative", "/scratch/a b", ""])
def test_bundle_transfer_rejects_invalid_remote_paths(
    portable_inputs: tuple[Path, Path, Path, SnapshotManifest], remote: str
) -> None:
    prepared = _stage(portable_inputs)
    with pytest.raises(GridOperatorError):
        build_bundle_transfer_argv(prepared, remote)


def test_result_retrieval_rejects_invalid_remote_shard_and_local_paths(
    tmp_path: Path,
) -> None:
    with pytest.raises(GridOperatorError, match="absolute path"):
        build_result_retrieval_argv("relative", tmp_path / "staging", SHARD)
    with pytest.raises(GridOperatorError, match=exactly("shard must be a Parquet path")):
        build_result_retrieval_argv("/scratch/run", tmp_path / "staging", "region.txt")

    local_file = tmp_path / "local-file"
    local_file.write_text("not a directory", encoding="utf-8")
    with pytest.raises(GridOperatorError, match="local staging directory"):
        build_result_retrieval_argv("/scratch/run", local_file, SHARD)

    real_directory = tmp_path / "real-staging"
    real_directory.mkdir()
    linked_directory = tmp_path / "linked-staging"
    linked_directory.symlink_to(real_directory, target_is_directory=True)
    with pytest.raises(GridOperatorError, match="local staging directory"):
        build_result_retrieval_argv("/scratch/run", linked_directory, SHARD)


def test_result_retrieval_accepts_root_remote_path_and_missing_local_directory(
    tmp_path: Path,
) -> None:
    argv = build_result_retrieval_argv("/", tmp_path / "new-staging", SHARD)

    assert argv[0] == "rsync"
    assert argv[-1].endswith("/")
    assert "/shards/" in argv[-2]
    assert not (tmp_path / "new-staging").exists()
