"""Adverse boundaries of the Grid operator that only error paths reach.

Some of these contracts are only reachable by calling a module-private helper
directly: the public entry points validate their inputs earlier. Keeping the
boundaries explicit gives the mutation gate a real oracle, and none of these
tests contacts a scheduler, a transport, or real data.
"""

import json
import os
import shutil
from dataclasses import replace
from datetime import UTC, datetime
from pathlib import Path

import pytest
from shapely.geometry import Polygon

from osm_polygon_description_tag.dataset.languages.checkpoint import shard_paths
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
    GridOperatorError,
    StagedFile,
    build_bundle_transfer_argv,
    bundle_for_shard,
    job_paths,
    prepare_job,
    prepare_portable_job,
    verify_prepared_bundle,
)
from tests.conftest import make_record_dict
from tests.helpers.messages import exactly

SHARD = "region.parquet"
REMOTE_BUNDLE = "/scratch/lang-bundle"
REMOTE = {
    "remote_project_dir": "/home/user/project",
    "remote_source_dir": "/scratch/staging/source",
    "remote_run_dir": "/scratch/staging/run",
}


def _write_project(project: Path, *, readme: bool = False) -> None:
    (project / "src").mkdir(parents=True)
    (project / "src" / "module.py").write_text("VALUE = 1\n", encoding="utf-8")
    (project / "pyproject.toml").write_text("[project]\nname = 'synthetic'\n", encoding="utf-8")
    (project / "uv.lock").write_text("version = 1\n", encoding="utf-8")
    if readme:
        (project / "README.md").write_text("# synthetic\n", encoding="utf-8")


@pytest.fixture
def inputs(tmp_path: Path) -> tuple[Path, Path, Path, SnapshotManifest]:
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
    _write_project(project)
    run = tmp_path / "run"
    snapshot = prepare_snapshot(
        source,
        run,
        code_fingerprint=fingerprint_project_source(project),
        lock_fingerprint=fingerprint_lockfile(project),
    )
    return project, source, run, snapshot


def _stage(inputs: tuple[Path, Path, Path, SnapshotManifest]) -> object:
    project, source, run, snapshot = inputs
    return prepare_portable_job(
        run, project, source, snapshot, SHARD, remote_bundle_dir=REMOTE_BUNDLE
    )


@pytest.mark.parametrize(
    ("relative_path", "message"),
    [
        (".", "must not contain traversal"),
        ("../escape", "must not contain traversal"),
        ("/absolute", "must be relative and portable"),
        ("", "must be a non-empty relative path"),
    ],
)
def test_a_staged_descriptor_must_name_a_portable_relative_file(
    relative_path: str, message: str
) -> None:
    with pytest.raises(GridOperatorError, match=message):
        StagedFile.from_payload(
            {"relative_path": relative_path, "size_bytes": 0, "sha256": "a" * 64}
        )


def test_a_remote_bundle_path_may_not_contain_traversal_components(
    inputs: tuple[Path, Path, Path, SnapshotManifest],
) -> None:
    prepared = _stage(inputs)

    with pytest.raises(GridOperatorError, match="traversal components"):
        build_bundle_transfer_argv(prepared, "/scratch/../lang-bundle")  # type: ignore[arg-type]


def test_repreparing_rejects_a_removed_immutable_config(
    inputs: tuple[Path, Path, Path, SnapshotManifest],
) -> None:
    _, _, run, snapshot = inputs
    _, paths = prepare_job(run, snapshot, SHARD, **REMOTE)
    paths.config.unlink()

    with pytest.raises(GridOperatorError, match="must be present"):
        prepare_job(run, snapshot, SHARD, **REMOTE)


def test_repreparing_rejects_a_removed_immutable_script(
    inputs: tuple[Path, Path, Path, SnapshotManifest],
) -> None:
    _, _, run, snapshot = inputs
    _, paths = prepare_job(run, snapshot, SHARD, **REMOTE)
    paths.script.unlink()

    with pytest.raises(GridOperatorError, match="its script must be present"):
        prepare_job(run, snapshot, SHARD, **REMOTE)


def test_a_symlinked_prepared_bundle_is_rejected(
    inputs: tuple[Path, Path, Path, SnapshotManifest], tmp_path: Path
) -> None:
    _, _, run, snapshot = inputs
    paths = job_paths(run, bundle_for_shard(snapshot, SHARD))
    paths.root.mkdir(parents=True)
    paths.bundle.symlink_to(tmp_path / "missing.json")

    with pytest.raises(GridOperatorError, match="must not be a symlink"):
        prepare_job(run, snapshot, SHARD, **REMOTE)


def test_an_unchanged_stable_payload_is_reused_without_rebuilding(
    inputs: tuple[Path, Path, Path, SnapshotManifest],
) -> None:
    first = _stage(inputs)
    stable = first.paths.payload_root  # type: ignore[attr-defined]
    shutil.copytree(first.payload_root, stable)  # type: ignore[attr-defined]

    second = _stage(inputs)

    assert second.payload_root == stable  # type: ignore[attr-defined]
    verify_prepared_bundle(stable)


def test_a_payload_without_a_stage_manifest_is_not_reused(
    inputs: tuple[Path, Path, Path, SnapshotManifest],
) -> None:
    first = _stage(inputs)
    stable = first.paths.payload_root  # type: ignore[attr-defined]
    stable.mkdir(parents=True)
    (stable / "leftover.txt").write_text("no stage manifest here\n", encoding="utf-8")

    second = _stage(inputs)

    assert second.payload_root != stable  # type: ignore[attr-defined]


def test_staging_a_non_regular_input_is_rejected(tmp_path: Path) -> None:
    with pytest.raises(GridOperatorError, match="must be a regular file"):
        grid_operator._copy_regular_file(tmp_path, tmp_path / "destination")


def test_an_optional_project_readme_is_staged(tmp_path: Path) -> None:
    source = tmp_path / "source"
    source.mkdir()
    write_geoparquet(
        iter(
            [
                make_record_dict(
                    Polygon([(0, 0), (0, 1), (1, 1), (1, 0)]),
                    {"description": "A described polygon"},
                )
            ]
        ),
        source / SHARD,
        batch_size=1,
    )
    project = tmp_path / "project"
    _write_project(project, readme=True)
    run = tmp_path / "run"
    snapshot = prepare_snapshot(
        source,
        run,
        code_fingerprint=fingerprint_project_source(project),
        lock_fingerprint=fingerprint_lockfile(project),
    )

    prepared = prepare_portable_job(
        run, project, source, snapshot, SHARD, remote_bundle_dir=REMOTE_BUNDLE
    )

    assert (prepared.project_root / "README.md").read_text(encoding="utf-8") == "# synthetic\n"


def test_a_missing_project_source_directory_is_rejected(tmp_path: Path) -> None:
    project = tmp_path / "project"
    project.mkdir()
    (project / "pyproject.toml").write_text("[project]\nname = 'x'\n", encoding="utf-8")
    (project / "uv.lock").write_text("version = 1\n", encoding="utf-8")

    with pytest.raises(GridOperatorError, match="project source directory is missing"):
        grid_operator._project_files(project)


def test_a_symlink_inside_the_project_source_is_rejected(tmp_path: Path) -> None:
    project = tmp_path / "project"
    _write_project(project)
    (project / "src" / "link.py").symlink_to(project / "src" / "module.py")

    with pytest.raises(GridOperatorError, match="project source contains a symlink"):
        grid_operator._project_files(project)


def test_a_non_file_inside_the_project_source_is_rejected(tmp_path: Path) -> None:
    project = tmp_path / "project"
    _write_project(project)
    os.mkfifo(project / "src" / "channel")

    with pytest.raises(GridOperatorError, match="project source contains a non-file"):
        grid_operator._project_files(project)


def test_staging_rejects_a_source_file_that_drifted_from_the_snapshot(
    inputs: tuple[Path, Path, Path, SnapshotManifest],
) -> None:
    project, source, run, snapshot = inputs
    write_geoparquet(
        iter(
            [
                make_record_dict(
                    Polygon([(0, 0), (0, 2), (2, 2), (2, 0)]),
                    {"description": "A different portable description"},
                )
            ]
        ),
        source / SHARD,
        batch_size=1,
    )

    with pytest.raises(GridOperatorError, match="does not match snapshot"):
        prepare_portable_job(run, project, source, snapshot, SHARD, remote_bundle_dir=REMOTE_BUNDLE)


def test_fsync_of_a_missing_directory_is_reported(tmp_path: Path) -> None:
    with pytest.raises(GridOperatorError, match="cannot fsync directory"):
        grid_operator._fsync_directory(tmp_path / "missing")


def test_a_policy_verdict_with_an_unusable_capture_instant_is_not_fresh() -> None:
    reason = grid_operator._policy_freshness_block(
        _verdict(captured_at=datetime.min.replace(tzinfo=UTC)),
        datetime(2026, 9, 8, tzinfo=UTC),
    )

    assert reason is not None


def _verdict(*, captured_at: datetime) -> object:
    from osm_polygon_description_tag.workflow.grid_policy import (
        PolicyDecision,
        PolicyEvidence,
        PolicyVerdict,
    )

    return PolicyVerdict(
        PolicyDecision.ALLOWED,
        ("test verdict",),
        PolicyEvidence(0, False, True, (), captured_at=captured_at),
    )


def test_a_symlinked_run_snapshot_is_rejected_when_copied(
    inputs: tuple[Path, Path, Path, SnapshotManifest], tmp_path: Path
) -> None:
    _, _, run, _ = inputs
    snapshot_path = run / SNAPSHOT_FILENAME
    outside = tmp_path / "outside.json"
    snapshot_path.rename(outside)
    snapshot_path.symlink_to(outside)

    with pytest.raises(GridOperatorError, match="must be a regular file"):
        grid_operator._copy_snapshot(run, tmp_path / "destination")


def test_a_symlinked_resume_checkpoint_is_refused_while_copying(
    inputs: tuple[Path, Path, Path, SnapshotManifest], tmp_path: Path
) -> None:
    _, _, run, _ = inputs
    state = shard_paths(run, SHARD)
    state.root.mkdir(parents=True)
    state.checkpoint.symlink_to(tmp_path / "missing.json")

    with pytest.raises(GridOperatorError, match=exactly("resume checkpoint must not be a symlink")):
        grid_operator._copy_resume_state(run, tmp_path / "destination", SHARD)


def test_copying_absent_resume_state_stages_nothing(
    inputs: tuple[Path, Path, Path, SnapshotManifest], tmp_path: Path
) -> None:
    _, _, run, _ = inputs
    destination = tmp_path / "destination"

    grid_operator._copy_resume_state(run, destination, SHARD)

    assert not destination.exists()


def test_a_stage_manifest_with_duplicate_files_is_rejected(
    inputs: tuple[Path, Path, Path, SnapshotManifest],
) -> None:
    prepared = _stage(inputs)
    manifest = prepared.manifest  # type: ignore[attr-defined]
    payload = json.loads(manifest.read_text(encoding="utf-8"))
    payload["files"].append(payload["files"][0])
    manifest.write_text(json.dumps(payload), encoding="utf-8")

    with pytest.raises(GridOperatorError, match=exactly("stage manifest contains duplicate files")):
        verify_prepared_bundle(prepared.payload_root)  # type: ignore[attr-defined]


def test_a_stage_manifest_missing_a_payload_file_is_rejected(
    inputs: tuple[Path, Path, Path, SnapshotManifest],
) -> None:
    prepared = _stage(inputs)
    manifest = prepared.manifest  # type: ignore[attr-defined]
    payload = json.loads(manifest.read_text(encoding="utf-8"))
    payload["files"] = [
        item for item in payload["files"] if item["relative_path"] != "job-config.json"
    ]
    manifest.write_text(json.dumps(payload), encoding="utf-8")

    with pytest.raises(
        GridOperatorError, match=exactly("stage manifest does not account for every payload file")
    ):
        verify_prepared_bundle(prepared.payload_root)  # type: ignore[attr-defined]


def _committed_run(
    inputs: tuple[Path, Path, Path, SnapshotManifest],
) -> tuple[Path, Path, SnapshotManifest]:
    """Process the shard once so a real committed history exists."""
    from osm_polygon_description_tag.dataset.languages.models import (
        LanguageResult,
        LanguageStatus,
    )
    from osm_polygon_description_tag.dataset.languages.worker import process_shard

    _, source, run, snapshot = inputs
    process_shard(
        run,
        source,
        SHARD,
        detector=lambda text: LanguageResult(
            "eng", 0.9, 0.1, 0.8, LanguageStatus.DETECTED, "detected"
        ),
        snapshot=snapshot,
        batch_size=1,
    )
    return source, run, snapshot


def test_copying_a_shard_outside_the_snapshot_is_rejected(
    inputs: tuple[Path, Path, Path, SnapshotManifest], tmp_path: Path
) -> None:
    _, source, _, snapshot = inputs

    with pytest.raises(GridOperatorError, match="not in snapshot"):
        grid_operator._copy_source_shard(snapshot, source, tmp_path / "out", "absent.parquet")


def test_resume_state_with_an_unexpected_artifact_is_rejected(
    inputs: tuple[Path, Path, Path, SnapshotManifest],
) -> None:
    _, run, _ = _committed_run(inputs)
    (shard_paths(run, SHARD).root / "notes.txt").write_text("stray\n", encoding="utf-8")

    with pytest.raises(GridOperatorError, match="unexpected artifact"):
        grid_operator._validate_resume_state(run, SHARD)


def test_resume_state_with_a_non_directory_parts_entry_is_rejected(
    inputs: tuple[Path, Path, Path, SnapshotManifest],
) -> None:
    _, run, _ = _committed_run(inputs)
    state = shard_paths(run, SHARD)
    shutil.rmtree(state.parts)
    state.parts.write_text("not a directory\n", encoding="utf-8")

    with pytest.raises(GridOperatorError, match="resume state directory is invalid"):
        grid_operator._validate_resume_state(run, SHARD)


def test_resume_state_that_fails_collection_validation_is_rejected(
    inputs: tuple[Path, Path, Path, SnapshotManifest],
) -> None:
    _, run, _ = _committed_run(inputs)
    state = shard_paths(run, SHARD)
    next(state.parts.iterdir()).write_bytes(b"not parquet")

    with pytest.raises(
        GridOperatorError, match=exactly("resume state failed collection validation")
    ):
        grid_operator._validate_resume_state(run, SHARD)


def test_resume_files_of_an_absent_state_root_are_empty(tmp_path: Path) -> None:
    assert grid_operator._resume_files(tmp_path / "missing") == ()


def test_resume_files_reject_a_symlink(tmp_path: Path) -> None:
    root = tmp_path / "state"
    root.mkdir()
    (root / "link.json").symlink_to(tmp_path / "missing.json")

    with pytest.raises(GridOperatorError, match="resume state contains a symlink"):
        grid_operator._resume_files(root)


def test_resume_files_reject_a_non_file(tmp_path: Path) -> None:
    root = tmp_path / "state"
    root.mkdir()
    os.mkfifo(root / "channel")

    with pytest.raises(GridOperatorError, match="resume state contains a non-file"):
        grid_operator._resume_files(root)


def test_a_malformed_stage_manifest_has_no_resume_fingerprint(tmp_path: Path) -> None:
    payload_root = tmp_path / "payload"
    payload_root.mkdir()
    (payload_root / "stage.json").write_text("[]", encoding="utf-8")

    assert grid_operator._staged_resume_fingerprint(payload_root) is None


def test_payload_files_reject_a_non_file(tmp_path: Path) -> None:
    payload_root = tmp_path / "payload"
    payload_root.mkdir()
    os.mkfifo(payload_root / "channel")

    with pytest.raises(GridOperatorError, match="portable payload contains a non-file"):
        grid_operator._payload_files(payload_root)


def test_a_staged_file_reached_through_a_symlinked_directory_is_rejected(
    inputs: tuple[Path, Path, Path, SnapshotManifest], tmp_path: Path
) -> None:
    prepared = _stage(inputs)
    root = prepared.payload_root  # type: ignore[attr-defined]
    outside = tmp_path / "outside"
    (root / "project").rename(outside)
    (root / "project").symlink_to(outside, target_is_directory=True)

    with pytest.raises(GridOperatorError, match="contains a symlink"):
        grid_operator._payload_file(root, "project/pyproject.toml")


def test_a_missing_staged_file_is_rejected(
    inputs: tuple[Path, Path, Path, SnapshotManifest],
) -> None:
    prepared = _stage(inputs)
    root = prepared.payload_root  # type: ignore[attr-defined]

    with pytest.raises(GridOperatorError, match="staged file is missing"):
        grid_operator._payload_file(root, "project/absent.toml")


def test_a_payload_without_its_top_level_directories_is_rejected(
    inputs: tuple[Path, Path, Path, SnapshotManifest],
) -> None:
    prepared = _stage(inputs)
    root = prepared.payload_root  # type: ignore[attr-defined]
    shutil.rmtree(root / "source")

    with pytest.raises(GridOperatorError, match="portable payload directory is invalid"):
        grid_operator._payload_directories(root)


def test_a_payload_without_project_source_code_is_rejected(
    inputs: tuple[Path, Path, Path, SnapshotManifest],
) -> None:
    prepared = _stage(inputs)
    root = prepared.payload_root  # type: ignore[attr-defined]
    shutil.rmtree(root / "project" / "src")

    with pytest.raises(
        GridOperatorError, match=exactly("portable payload is missing project source code")
    ):
        grid_operator._verify_payload_layout(root, root / "project", prepared.bundle)  # type: ignore[attr-defined]


def test_a_non_executable_payload_job_script_is_rejected(
    inputs: tuple[Path, Path, Path, SnapshotManifest],
) -> None:
    prepared = _stage(inputs)
    root = prepared.payload_root  # type: ignore[attr-defined]
    (root / "job.sh").chmod(0o400)

    with pytest.raises(GridOperatorError, match=exactly("portable job script is not executable")):
        grid_operator._verify_payload_layout(root, root / "project", prepared.bundle)  # type: ignore[attr-defined]


def test_a_payload_with_an_unreadable_snapshot_is_rejected(
    inputs: tuple[Path, Path, Path, SnapshotManifest],
) -> None:
    prepared = _stage(inputs)
    root = prepared.payload_root  # type: ignore[attr-defined]
    (root / "run" / SNAPSHOT_FILENAME).write_bytes(b"not JSON")

    with pytest.raises(GridOperatorError):
        grid_operator._verify_payload_identity(
            root / "project",
            root / "source",
            root / "run",
            prepared.bundle,  # type: ignore[attr-defined]
        )


@pytest.mark.parametrize(
    ("field", "message"),
    [
        ("code_fingerprint", "staged snapshot code fingerprint does not match bundle"),
        ("lock_fingerprint", "staged snapshot lock fingerprint does not match bundle"),
    ],
)
def test_a_staged_snapshot_with_foreign_fingerprints_is_rejected(
    inputs: tuple[Path, Path, Path, SnapshotManifest], field: str, message: str
) -> None:
    _, _, _, snapshot = inputs
    foreign = replace(bundle_for_shard(snapshot, SHARD), **{field: "c" * 64})

    with pytest.raises(GridOperatorError, match=exactly(message)):
        grid_operator._validate_staged_snapshot(snapshot, foreign)


@pytest.mark.parametrize("target", ["src", "uv.lock"])
def test_a_staged_project_that_drifted_from_the_bundle_is_rejected(
    inputs: tuple[Path, Path, Path, SnapshotManifest], target: str
) -> None:
    prepared = _stage(inputs)
    project_root = prepared.project_root  # type: ignore[attr-defined]
    if target == "src":
        (project_root / "src" / "module.py").write_text("VALUE = 2\n", encoding="utf-8")
        message = "staged project source does not match bundle"
    else:
        (project_root / "uv.lock").write_text("version = 2\n", encoding="utf-8")
        message = "staged lockfile does not match bundle"

    with pytest.raises(GridOperatorError, match=exactly(message)):
        grid_operator._validate_staged_project(project_root, prepared.bundle)  # type: ignore[attr-defined]


def test_a_payload_with_more_than_one_input_shard_is_rejected(
    inputs: tuple[Path, Path, Path, SnapshotManifest],
) -> None:
    prepared = _stage(inputs)
    source_root = prepared.source_root  # type: ignore[attr-defined]
    (source_root / "extra.parquet").write_bytes(b"extra")

    with pytest.raises(
        GridOperatorError, match=exactly("portable payload must contain exactly one input shard")
    ):
        grid_operator._verify_one_staged_input(source_root, prepared.bundle)  # type: ignore[attr-defined]


def test_validating_absent_resume_state_reports_nothing(
    inputs: tuple[Path, Path, Path, SnapshotManifest],
) -> None:
    _, _, run, _ = inputs

    assert grid_operator._validate_resume_state(run, SHARD) is None


def test_retrieved_results_from_another_snapshot_are_rejected(
    inputs: tuple[Path, Path, Path, SnapshotManifest], tmp_path: Path
) -> None:
    _, source, run, _ = inputs
    other = tmp_path / "other-run"
    prepare_snapshot(source, other, code_fingerprint="d" * 64, lock_fingerprint="e" * 64)

    with pytest.raises(
        GridOperatorError, match=exactly("retrieved results belong to a different snapshot")
    ):
        grid_operator._require_matching_snapshots(run, other)


def test_an_unreadable_retrieved_snapshot_is_rejected(
    inputs: tuple[Path, Path, Path, SnapshotManifest], tmp_path: Path
) -> None:
    _, _, run, _ = inputs
    other = tmp_path / "other-run"
    other.mkdir()
    (other / SNAPSHOT_FILENAME).write_bytes(b"not JSON")

    with pytest.raises(GridOperatorError):
        grid_operator._require_matching_snapshots(run, other)


def test_a_non_directory_retrieved_run_is_rejected(tmp_path: Path) -> None:
    path = tmp_path / "not-a-directory"
    path.write_text("file\n", encoding="utf-8")

    with pytest.raises(GridOperatorError, match="retrieved run directory is not a regular"):
        grid_operator._require_regular_directory(path, "retrieved run directory")


def test_an_unavailable_retrieved_shard_directory_is_rejected(tmp_path: Path) -> None:
    with pytest.raises(GridOperatorError, match="retrieved shard directory is unavailable"):
        grid_operator._reject_overlapping_paths(tmp_path / "missing", tmp_path / "local")


def test_a_symlinked_local_result_directory_is_rejected(tmp_path: Path) -> None:
    from osm_polygon_description_tag.dataset.languages.checkpoint import ShardPaths

    root = tmp_path / "state"
    root.mkdir()
    parts = root / "parts"
    parts.symlink_to(tmp_path / "outside", target_is_directory=True)
    paths = ShardPaths(root, parts, root / "receipts", root / "checkpoint.json")

    with pytest.raises(GridOperatorError, match="must not be a symlink"):
        grid_operator._prepare_collection_directories(paths)


def test_intents_of_a_run_without_a_jobs_directory_are_empty(tmp_path: Path) -> None:
    assert list(grid_operator._iter_intents(tmp_path)) == []


def test_a_jobs_path_that_is_not_a_directory_is_rejected(tmp_path: Path) -> None:
    (tmp_path / "jobs").write_text("not a directory\n", encoding="utf-8")

    with pytest.raises(GridOperatorError, match="jobs directory is not a regular directory"):
        list(grid_operator._iter_intents(tmp_path))


def test_a_lock_path_that_cannot_be_opened_is_reported(tmp_path: Path) -> None:
    lock_path = tmp_path / "missing-directory" / "lock"

    with pytest.raises(GridOperatorError, match="cannot open submission lock"):
        grid_operator._open_lock(lock_path)


def test_an_unexpected_lock_error_is_reported(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import errno
    import fcntl

    handle = (tmp_path / "lock").open("a+")

    def refuse(descriptor: int, operation: int) -> None:
        raise OSError(errno.ENOLCK, "no locks available")

    monkeypatch.setattr(fcntl, "flock", refuse)
    try:
        with pytest.raises(GridOperatorError, match="cannot acquire submission lock"):
            grid_operator._acquire_lock(handle, tmp_path)
    finally:
        handle.close()


def test_a_naive_evaluation_instant_makes_freshness_unknown() -> None:
    reason = grid_operator._policy_freshness_block(
        _verdict(captured_at=datetime(2026, 9, 8, tzinfo=UTC)),
        # A naive evaluation instant is not comparable and must fail closed.
        datetime(2026, 9, 8),
    )

    assert reason == "policy evidence freshness is unknown"


def _shard_report(**changes: object) -> object:
    from osm_polygon_description_tag.dataset.languages.validation import ShardReport

    defaults: dict[str, object] = {
        "shard": SHARD,
        "status": "paused",
        "input_row_count": 1,
        "input_cursor": 0,
        "annotation_count": 0,
        "part_count": 0,
        "issues": (),
    }
    return ShardReport(**{**defaults, **changes})  # type: ignore[arg-type]


def _run_report(selected: object, **changes: object) -> object:
    from osm_polygon_description_tag.dataset.languages.validation import RunReport

    defaults: dict[str, object] = {
        "snapshot_id": "a" * 64,
        "model_config_fingerprint": "b" * 64,
        "shard_count": 1,
        "complete_shard_count": 0,
        "annotation_count": 0,
        "input_row_count": 1,
        "shards": (selected,),
        "issues": (),
    }
    return RunReport(**{**defaults, **changes})  # type: ignore[arg-type]


def test_collected_results_for_another_shard_are_rejected(
    inputs: tuple[Path, Path, Path, SnapshotManifest],
) -> None:
    _, _, _, snapshot = inputs
    bundle = bundle_for_shard(snapshot, SHARD)
    selected = _shard_report(shard="other.parquet")
    report = _run_report(selected, snapshot_id=bundle.snapshot_id)

    with pytest.raises(
        GridOperatorError, match=exactly("collected results do not match the submitted bundle")
    ):
        grid_operator._validate_ack_identity(report, selected, bundle)  # type: ignore[arg-type]


def test_a_shard_without_a_terminal_checkpoint_cannot_be_acknowledged() -> None:
    from osm_polygon_description_tag.workflow.grid_operator import SubmissionIntent

    intent = SubmissionIntent(
        bundle_id="a" * 64,
        shard=SHARD,
        job_name="lang-aaaaaaaaaaaaaaaa",
        walltime_seconds=1800,
        cores=1,
        recorded_at="2026-09-08T12:00:00+00:00",
        terminal_state="terminated",
    )

    with pytest.raises(
        GridOperatorError, match=exactly("collected results do not have a terminal checkpoint")
    ):
        grid_operator._validate_ack_terminal(_shard_report(status="missing"), intent)  # type: ignore[arg-type]


def test_complete_results_cannot_be_downgraded_to_paused(
    inputs: tuple[Path, Path, Path, SnapshotManifest],
) -> None:
    from osm_polygon_description_tag.workflow.grid_operator import SubmissionIntent

    _, _, _, snapshot = inputs
    bundle = bundle_for_shard(snapshot, SHARD)
    intent = SubmissionIntent(
        bundle_id="a" * 64,
        shard=SHARD,
        job_name="lang-aaaaaaaaaaaaaaaa",
        walltime_seconds=1800,
        cores=1,
        recorded_at="2026-09-08T12:00:00+00:00",
        terminal_state="terminated",
        result_acknowledged=True,
        result_complete=True,
    )
    selected = _shard_report()
    report = _run_report(selected, snapshot_id=bundle.snapshot_id)

    with pytest.raises(
        GridOperatorError, match=exactly("complete results cannot be downgraded to paused results")
    ):
        grid_operator._validate_acknowledgment(report, selected, intent, bundle)  # type: ignore[arg-type]


def test_the_run_wide_scan_skips_this_job_and_keeps_inspecting_the_rest(
    inputs: tuple[Path, Path, Path, SnapshotManifest],
) -> None:
    from osm_polygon_description_tag.workflow.grid_operator import (
        BUNDLE_FILENAME,
        INTENT_FILENAME,
        SubmissionIntent,
    )

    _, _, run, snapshot = inputs
    bundle, paths = prepare_job(run, snapshot, SHARD, **REMOTE)
    own = SubmissionIntent(
        bundle_id=bundle.bundle_id,
        shard=SHARD,
        job_name="lang-own",
        walltime_seconds=1800,
        cores=1,
        recorded_at="2026-09-08T12:00:00+00:00",
        job_id=11,
        terminal_state="terminated",
        result_acknowledged=True,
    )
    paths.intent.write_text(json.dumps(own.to_payload()), encoding="utf-8")
    foreign_bundle = replace(bundle, source_sha256="c" * 64)
    other = run / "jobs" / "zz-other"
    other.mkdir(parents=True)
    (other / BUNDLE_FILENAME).write_text(json.dumps(foreign_bundle.to_payload()), encoding="utf-8")
    (other / INTENT_FILENAME).write_text(
        json.dumps(replace(own, bundle_id=foreign_bundle.bundle_id).to_payload()),
        encoding="utf-8",
    )

    assert grid_operator._run_wide_intent_block(paths, bundle) is None


def test_planning_rejects_a_bundle_that_is_not_the_prepared_one(
    inputs: tuple[Path, Path, Path, SnapshotManifest],
) -> None:
    _, _, run, snapshot = inputs
    bundle, paths = prepare_job(run, snapshot, SHARD, **REMOTE)

    with pytest.raises(
        GridOperatorError, match=exactly("prepared bundle does not match the requested submission")
    ):
        grid_operator._validate_prepared_submission(
            paths, replace(bundle, source_sha256="c" * 64), 1800
        )


@pytest.mark.parametrize("walltime", [0, -1, True, 1.5, "1800"])
def test_planning_rejects_a_walltime_that_is_not_a_positive_integer(walltime: object) -> None:
    with pytest.raises(GridOperatorError, match="must be a positive number of seconds"):
        grid_operator._validate_submission_walltime(walltime)
