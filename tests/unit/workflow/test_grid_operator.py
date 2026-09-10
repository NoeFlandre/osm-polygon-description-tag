"""Preparing, gating, submitting, reconciling, and collecting one Grid'5000 job."""

import json
import os
import shutil
import stat
import subprocess
import sys
from collections.abc import Sequence
from dataclasses import replace
from datetime import UTC, datetime
from pathlib import Path

import matplotlib
import pytest
from shapely.geometry import Polygon

from osm_polygon_description_tag.dataset.languages.checkpoint import (
    ShardCheckpoint,
    ShardStatus,
    WorkerBusyError,
    exclusive_worker_lock,
    read_checkpoint,
    shard_paths,
    write_checkpoint,
)
from osm_polygon_description_tag.dataset.languages.models import (
    LanguageResult,
    LanguageStatus,
    cascade_model_identity,
)
from osm_polygon_description_tag.dataset.languages.snapshot import (
    SnapshotManifest,
    fingerprint_lockfile,
    fingerprint_project_source,
    prepare_snapshot,
)
from osm_polygon_description_tag.dataset.languages.worker import ProcessingBudget, process_shard
from osm_polygon_description_tag.storage import write_geoparquet
from osm_polygon_description_tag.workflow.grid_operator import (
    GridOperatorError,
    JobBundle,
    JobPaths,
    SubmissionIntent,
    acknowledge_collected_results,
    build_bundle_transfer_argv,
    build_result_retrieval_argv,
    bundle_for_shard,
    collect_results,
    gather_policy_evidence,
    import_retrieved_results,
    job_paths,
    plan_submission,
    prepare_job,
    prepare_portable_job,
    read_bundle,
    read_intent,
    reconcile_job,
    render_job_script,
    resolve_job_name,
    submit_job,
    verify_prepared_bundle,
)
from osm_polygon_description_tag.workflow.grid_policy import (
    GRID5000_TIMEZONE,
    MAX_PROCESSING_SECONDS,
    MAX_WALLTIME_SECONDS,
    PolicyDecision,
    PolicyEvidence,
    PolicyVerdict,
)
from osm_polygon_description_tag.workflow.grid_scheduler import (
    CommandResult,
    JobState,
    SubmissionOutcome,
)
from tests.conftest import make_record_dict

SHARD = "region.parquet"
REMOTE = {
    "remote_project_dir": "/home/user/project",
    "remote_source_dir": "/scratch/staging/source",
    "remote_run_dir": "/scratch/staging/run",
}


def _verdict(
    decision: PolicyDecision = PolicyDecision.ALLOWED, *, captured_at: datetime | None = None
) -> PolicyVerdict:
    return PolicyVerdict(
        decision,
        ("test verdict",),
        PolicyEvidence(0, False, True, (), captured_at=captured_at or datetime.now(UTC)),
    )


@pytest.fixture
def prepared(tmp_path: Path) -> tuple[Path, Path, SnapshotManifest]:
    source = tmp_path / "source"
    records = (
        make_record_dict(
            Polygon([(0, 0), (0, 1), (1, 1), (1, 0)]),
            {"description": f"A synthetic description {index}"},
            osm_id=index + 1,
        )
        for index in range(4)
    )
    source.mkdir(parents=True)
    write_geoparquet(records, source / SHARD, batch_size=2)
    run = tmp_path / "run"
    snapshot = prepare_snapshot(source, run, code_fingerprint="a" * 64, lock_fingerprint="b" * 64)
    return source, run, snapshot


@pytest.fixture
def portable_prepared(tmp_path: Path) -> tuple[Path, Path, Path, SnapshotManifest]:
    source = tmp_path / "source"
    source.mkdir(parents=True)
    write_geoparquet(
        (
            make_record_dict(
                Polygon([(0, 0), (0, 1), (1, 1), (1, 0)]),
                {"description": f"A portable description {index}"},
                osm_id=index + 1,
            )
            for index in range(4)
        ),
        source / SHARD,
        batch_size=2,
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


def _two_shard_prepared(tmp_path: Path) -> tuple[Path, SnapshotManifest, dict[str, Path]]:
    source = tmp_path / "source"
    source.mkdir(parents=True)
    for shard in (SHARD, "other.parquet"):
        write_geoparquet(
            (
                make_record_dict(
                    Polygon([(0, 0), (0, 1), (1, 1), (1, 0)]),
                    {"description": f"A synthetic description {index}"},
                    osm_id=index + 1,
                )
                for index in range(2)
            ),
            source / shard,
            batch_size=1,
        )
    run = tmp_path / "run"
    snapshot = prepare_snapshot(source, run, code_fingerprint="a" * 64, lock_fingerprint="b" * 64)
    return run, snapshot, {"source": source, "run": run}


def _runner(results: list[CommandResult], observed: list[tuple[str, ...]] | None = None) -> object:
    def _run(argv: Sequence[str], timeout: float) -> CommandResult:
        if observed is not None:
            observed.append(tuple(argv))
        return results.pop(0)

    return _run


def test_a_bundle_binds_code_lock_and_one_shard(
    prepared: tuple[Path, Path, SnapshotManifest],
) -> None:
    _, _, snapshot = prepared

    bundle = bundle_for_shard(snapshot, SHARD)

    assert bundle.snapshot_id == snapshot.snapshot_id
    assert bundle.code_fingerprint == "a" * 64
    assert bundle.lock_fingerprint == "b" * 64
    assert bundle.shard == SHARD
    assert bundle.input_row_count == 4
    assert len(bundle.bundle_id) == 64
    assert bundle == bundle_for_shard(snapshot, SHARD)


def test_a_bundle_round_trips_and_rejects_a_tampered_identity(
    prepared: tuple[Path, Path, SnapshotManifest],
) -> None:
    _, _, snapshot = prepared
    payload = bundle_for_shard(snapshot, SHARD).to_payload()

    assert JobBundle.from_payload(payload) == bundle_for_shard(snapshot, SHARD)

    with pytest.raises(GridOperatorError, match="does not match its canonical content"):
        JobBundle.from_payload({**payload, "input_row_count": 99})
    with pytest.raises(GridOperatorError, match="unsupported bundle schema version"):
        JobBundle.from_payload({**payload, "bundle_schema_version": 2})
    with pytest.raises(GridOperatorError, match="must be a string"):
        JobBundle.from_payload({**payload, "shard": 7})
    with pytest.raises(GridOperatorError, match="payload must be an object"):
        JobBundle.from_payload([])


def test_the_job_script_bounds_threads_and_installs_from_the_lockfile(
    prepared: tuple[Path, Path, SnapshotManifest],
) -> None:
    _, _, snapshot = prepared

    script = render_job_script(bundle_for_shard(snapshot, SHARD), **REMOTE)

    assert script.startswith("#!/usr/bin/env bash")
    assert "set -euo pipefail" in script
    assert "export OMP_NUM_THREADS=1" in script
    assert "export RAYON_NUM_THREADS=1" in script
    assert 'export UV_PYTHON_INSTALL_DIR="$job_tmp_root/python"' in script
    assert 'export XDG_DATA_HOME="$job_tmp_root/xdg-data"' in script
    assert "uv sync --frozen --no-dev --extra language" in script
    assert "uv run --no-sync" in script
    assert "language run" in script
    assert "language validate" in script
    assert f"--budget-seconds {MAX_PROCESSING_SECONDS}" in script
    assert "--shard region.parquet" in script
    assert "verify_prepared_bundle" not in script


def test_bare_prepared_job_script_runs_without_a_portable_payload(
    prepared: tuple[Path, Path, SnapshotManifest],
    tmp_path: Path,
) -> None:
    _, run, snapshot = prepared
    bundle, paths = prepare_job(
        run,
        snapshot,
        SHARD,
        remote_project_dir=str(tmp_path / "project"),
        remote_source_dir=str(tmp_path / "source"),
        remote_run_dir=str(tmp_path / "run-output"),
    )
    for directory in (tmp_path / "project", tmp_path / "source", tmp_path / "run-output"):
        directory.mkdir(exist_ok=True)

    fake_bin = tmp_path / "fake-bin"
    fake_bin.mkdir()
    fake_uv = fake_bin / "uv"
    fake_uv.write_text(
        """#!/usr/bin/env bash
set -euo pipefail
if [[ "$1" == "sync" ]]; then
  test -n "${UV_PROJECT_ENVIRONMENT:-}"
  test "$UV_PROJECT_ENVIRONMENT" != "$PWD/.venv"
  test "$UV_PYTHON_INSTALL_DIR" == "${UV_PROJECT_ENVIRONMENT%/venv}/python"
  test "$XDG_DATA_HOME" == "${UV_PROJECT_ENVIRONMENT%/venv}/xdg-data"
  test "$PYTHONPYCACHEPREFIX" == "${UV_PROJECT_ENVIRONMENT%/venv}/pycache"
  test "$PYTHONDONTWRITEBYTECODE" == "1"
  test "$MPLCONFIGDIR" == "${UV_PROJECT_ENVIRONMENT%/venv}/matplotlib"
  mkdir -p "$UV_PROJECT_ENVIRONMENT/bin"
  ln -s /bin/sh "$UV_PROJECT_ENVIRONMENT/bin/sh"
  exit 0
fi
if [[ "$1" == "run" && "$2" == "--no-sync" ]]; then
  exit 0
fi
exit 91
""",
        encoding="utf-8",
    )
    fake_uv.chmod(0o700)
    job_tmp = tmp_path / "job-tmp"
    job_tmp.mkdir()
    environment = {
        **os.environ,
        "PATH": f"{fake_bin}{os.pathsep}{os.environ['PATH']}",
        "TMPDIR": str(job_tmp),
    }
    bash = shutil.which("bash")
    assert bash is not None

    subprocess.run(  # noqa: S603 - executes the synthetic generated fixture
        [bash, str(paths.script)],
        check=True,
        capture_output=True,
        text=True,
        env=environment,
    )

    assert bundle == read_bundle(paths.bundle)
    assert "verify_prepared_bundle" not in paths.script.read_text(encoding="utf-8")
    assert not tuple(job_tmp.iterdir())


def test_the_job_script_quotes_paths_and_requires_processing_inside_walltime(
    prepared: tuple[Path, Path, SnapshotManifest],
) -> None:
    _, _, snapshot = prepared
    bundle = bundle_for_shard(snapshot, SHARD)

    script = render_job_script(
        bundle,
        remote_project_dir="/scratch/project-safe",
        remote_source_dir="/scratch/source-safe",
        remote_run_dir="/scratch/run-safe",
        remote_bundle_dir="/scratch/bundle-safe",
        walltime_seconds=MAX_PROCESSING_SECONDS + 1,
    )

    assert "cd /scratch/project-safe" in script
    assert "--source-root /scratch/source-safe" in script
    assert "--run-dir /scratch/run-safe" in script
    assert "--shard region.parquet" in script
    assert "verify_prepared_bundle" in script

    with pytest.raises(GridOperatorError, match="must be less than walltime"):
        render_job_script(
            bundle,
            **REMOTE,
            walltime_seconds=MAX_PROCESSING_SECONDS,
        )

    with pytest.raises(GridOperatorError, match="shell metacharacters"):
        unsafe_run_dir = "/tmp/run" + ";touch"  # noqa: S108 - deliberate unsafe fixture
        render_job_script(bundle, **{**REMOTE, "remote_run_dir": unsafe_run_dir})


def test_a_portable_job_contains_code_lock_input_and_resume_state(
    portable_prepared: tuple[Path, Path, Path, SnapshotManifest],
) -> None:
    project, source, run, snapshot = portable_prepared
    process_shard(
        run,
        source,
        SHARD,
        detector=lambda text: LanguageResult(
            "eng", 0.9, 0.1, 0.8, LanguageStatus.DETECTED, "detected"
        ),
        snapshot=snapshot,
        batch_size=2,
    )

    prepared_bundle = prepare_portable_job(
        run,
        project,
        source,
        snapshot,
        SHARD,
        remote_bundle_dir="/scratch/lang-bundle",
    )

    root = prepared_bundle.payload_root
    assert (root / "bundle.json").is_file()
    assert (root / "stage.json").is_file()
    assert (root / "project" / "pyproject.toml").is_file()
    assert (root / "project" / "uv.lock").is_file()
    assert (root / "project" / "src" / "module.py").read_text(encoding="utf-8") == "VALUE = 1\n"
    assert (root / "source" / SHARD).read_bytes() == (source / SHARD).read_bytes()
    assert (root / "run" / "snapshot.json").is_file()
    staged_shard = root / "run" / "shards" / shard_paths(root / "run", SHARD).root.name
    assert staged_shard.is_dir()

    shard_root = root / "run" / "shards"
    assert any(path.name == "checkpoint.json" for path in shard_root.rglob("*"))
    verify_prepared_bundle(root)


def test_generated_script_runs_verifier_after_external_fake_uv_environment(
    portable_prepared: tuple[Path, Path, Path, SnapshotManifest],
    tmp_path: Path,
) -> None:
    project, source, run, snapshot = portable_prepared
    prepared = prepare_portable_job(
        run,
        project,
        source,
        snapshot,
        SHARD,
        remote_bundle_dir=str(tmp_path / "remote-bundle"),
    )
    remote_bundle = tmp_path / "remote-bundle"
    shutil.copytree(prepared.payload_root, remote_bundle)

    fake_bin = tmp_path / "fake-bin"
    fake_bin.mkdir()
    fake_log = tmp_path / "fake-uv.log"
    fake_uv = fake_bin / "uv"
    fake_uv.write_text(
        """#!/usr/bin/env bash
set -euo pipefail
printf '%s\\n' "$*" >> "$UV_FAKE_LOG"
if [[ "$1" == "sync" ]]; then
  test -n "${UV_PROJECT_ENVIRONMENT:-}"
  test "$UV_PROJECT_ENVIRONMENT" != "$PWD/.venv"
  test "$UV_PYTHON_INSTALL_DIR" == "${UV_PROJECT_ENVIRONMENT%/venv}/python"
  test "$XDG_DATA_HOME" == "${UV_PROJECT_ENVIRONMENT%/venv}/xdg-data"
  mkdir -p "$UV_PROJECT_ENVIRONMENT/bin"
  ln -s /bin/sh "$UV_PROJECT_ENVIRONMENT/bin/sh"
  exit 0
fi
if [[ "$1" != "run" || "$2" != "--no-sync" ]]; then
  exit 91
fi
shift 2
if [[ "$1" == "python" ]]; then
  shift
  test "$1" == "-c"
  shift
  verifier="$1"
  shift
  test "$#" -eq 1
  # Reuse dependency caches after checking the real job's isolation settings.
  unset PYTHONPYCACHEPREFIX
  MPLCONFIGDIR="$UV_FAKE_MPLCONFIGDIR" "$UV_FAKE_PYTHON" -c "$verifier" "$1"
fi
""",
        encoding="utf-8",
    )
    fake_uv.chmod(0o700)
    job_tmp = tmp_path / "job-tmp"
    job_tmp.mkdir()
    source_root = Path(__file__).resolve().parents[3] / "src"
    environment = {
        **os.environ,
        "PATH": f"{fake_bin}{os.pathsep}{os.environ['PATH']}",
        "PYTHONPATH": f"{source_root}{os.pathsep}{os.environ.get('PYTHONPATH', '')}",
        "TMPDIR": str(job_tmp),
        "UV_FAKE_LOG": str(fake_log),
        "UV_FAKE_PYTHON": sys.executable,
        "UV_FAKE_MPLCONFIGDIR": matplotlib.get_cachedir(),
    }

    bash = shutil.which("bash")
    assert bash is not None
    result = subprocess.run(  # noqa: S603 - executes the synthetic generated fixture
        [bash, str(prepared.paths.script)],
        check=False,
        capture_output=True,
        text=True,
        env=environment,
    )
    assert result.returncode == 0, result.stderr

    log = fake_log.read_text(encoding="utf-8")
    assert "sync --frozen --no-dev --extra language" in log
    assert "run --no-sync python" in log
    assert not (remote_bundle / "project" / ".venv").exists()
    assert not tuple(job_tmp.iterdir())
    verify_prepared_bundle(remote_bundle)


def test_portable_payload_rebuilds_when_resume_state_advances(
    portable_prepared: tuple[Path, Path, Path, SnapshotManifest],
) -> None:
    project, source, run, snapshot = portable_prepared
    first = prepare_portable_job(
        run,
        project,
        source,
        snapshot,
        SHARD,
        remote_bundle_dir="/scratch/lang-bundle",
    )
    process_shard(
        run,
        source,
        SHARD,
        detector=lambda text: LanguageResult(
            "eng", 0.9, 0.1, 0.8, LanguageStatus.DETECTED, "detected"
        ),
        snapshot=snapshot,
        batch_size=2,
        budget=ProcessingBudget(1, clock=iter([0.0, 0.0, 0.0, 3.0]).__next__),
    )

    second = prepare_portable_job(
        run,
        project,
        source,
        snapshot,
        SHARD,
        remote_bundle_dir="/scratch/lang-bundle",
    )

    assert first.payload_root != second.payload_root
    first_shard_root = shard_paths(first.run_root, SHARD).root.name
    second_shard_root = shard_paths(second.run_root, SHARD).root.name
    first_checkpoint = first.run_root / "shards" / first_shard_root / "checkpoint.json"
    second_checkpoint = second.run_root / "shards" / second_shard_root / "checkpoint.json"
    assert read_checkpoint(first_checkpoint).input_cursor == 0
    assert read_checkpoint(second_checkpoint).input_cursor > 0
    verify_prepared_bundle(first.payload_root)
    verify_prepared_bundle(second.payload_root)


def test_retrieved_results_are_validated_before_atomic_local_promotion(
    portable_prepared: tuple[Path, Path, Path, SnapshotManifest],
    tmp_path: Path,
) -> None:
    project, source, run, snapshot = portable_prepared
    process_shard(
        run,
        source,
        SHARD,
        detector=lambda text: LanguageResult(
            "eng", 0.9, 0.1, 0.8, LanguageStatus.DETECTED, "detected"
        ),
        snapshot=snapshot,
        batch_size=2,
    )
    prepared_bundle = prepare_portable_job(
        run,
        project,
        source,
        snapshot,
        SHARD,
        remote_bundle_dir="/scratch/lang-bundle",
    )
    local_run = tmp_path / "local-run"
    prepare_snapshot(
        source,
        local_run,
        code_fingerprint=fingerprint_project_source(project),
        lock_fingerprint=fingerprint_lockfile(project),
    )

    report = import_retrieved_results(local_run, prepared_bundle.run_root, SHARD)

    assert report.is_complete
    assert collect_results(local_run, SHARD).is_complete


def _process_collection_fixture(
    source: Path,
    run: Path,
    snapshot: SnapshotManifest,
    *,
    language: str = "eng",
    budget: ProcessingBudget | None = None,
) -> None:
    process_shard(
        run,
        source,
        SHARD,
        detector=lambda text: LanguageResult(
            language, 0.9, 0.1, 0.8, LanguageStatus.DETECTED, "detected"
        ),
        snapshot=snapshot,
        batch_size=2,
        budget=budget,
    )


@pytest.fixture
def collection_runs(
    prepared: tuple[Path, Path, SnapshotManifest], tmp_path: Path
) -> tuple[Path, Path, Path, SnapshotManifest]:
    source, local, snapshot = prepared
    incoming = tmp_path / "retrieved"
    prepare_snapshot(
        source,
        incoming,
        code_fingerprint=snapshot.code_fingerprint,
        lock_fingerprint=snapshot.lock_fingerprint,
    )
    return source, local, incoming, snapshot


@pytest.mark.parametrize("fault", ["unexpected_shard", "uncommitted_part"])
def test_collection_does_not_report_success_with_invalid_local_artifacts(
    collection_runs: tuple[Path, Path, Path, SnapshotManifest], fault: str
) -> None:
    source, local, incoming, snapshot = collection_runs
    _process_collection_fixture(source, incoming, snapshot)
    if fault == "unexpected_shard":
        (local / "shards" / "foreign").mkdir(parents=True)
    else:
        parts = shard_paths(local, SHARD).parts
        parts.mkdir(parents=True)
        (parts / "part-99999999.parquet").write_bytes(b"uncommitted")

    with pytest.raises(GridOperatorError, match="imported results failed local"):
        import_retrieved_results(local, incoming, SHARD)


def test_collection_refuses_older_progress_without_touching_complete_results(
    collection_runs: tuple[Path, Path, Path, SnapshotManifest],
) -> None:
    source, local, incoming, snapshot = collection_runs
    _process_collection_fixture(source, local, snapshot)
    _process_collection_fixture(
        source, incoming, snapshot, budget=ProcessingBudget(1, clock=iter([0.0, 2.0]).__next__)
    )
    checkpoint = shard_paths(local, SHARD).checkpoint
    before = checkpoint.read_bytes()

    with pytest.raises(GridOperatorError, match="regress"):
        import_retrieved_results(local, incoming, SHARD)

    assert checkpoint.read_bytes() == before
    assert collect_results(local, SHARD).is_complete


def test_collection_refuses_conflicting_committed_history(
    collection_runs: tuple[Path, Path, Path, SnapshotManifest],
) -> None:
    source, local, incoming, snapshot = collection_runs
    _process_collection_fixture(source, local, snapshot, language="eng")
    _process_collection_fixture(source, incoming, snapshot, language="fra")
    paths = shard_paths(local, SHARD)
    before = {path.name: path.read_bytes() for path in paths.parts.iterdir()}

    with pytest.raises(GridOperatorError, match="committed history"):
        import_retrieved_results(local, incoming, SHARD)

    assert {path.name: path.read_bytes() for path in paths.parts.iterdir()} == before


def test_collection_cannot_replace_state_under_a_running_worker(
    collection_runs: tuple[Path, Path, Path, SnapshotManifest],
) -> None:
    source, local, incoming, snapshot = collection_runs
    _process_collection_fixture(source, incoming, snapshot)

    with exclusive_worker_lock(local), pytest.raises(WorkerBusyError):
        import_retrieved_results(local, incoming, SHARD)

    assert not shard_paths(local, SHARD).checkpoint.exists()


def test_interrupted_collection_keeps_the_old_checkpoint_and_can_retry(
    collection_runs: tuple[Path, Path, Path, SnapshotManifest],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from osm_polygon_description_tag.workflow import grid_operator

    source, local, incoming, snapshot = collection_runs
    _process_collection_fixture(
        source, local, snapshot, budget=ProcessingBudget(1, clock=iter([0.0, 2.0]).__next__)
    )
    _process_collection_fixture(source, incoming, snapshot)
    checkpoint = shard_paths(local, SHARD).checkpoint
    before = checkpoint.read_bytes()
    original = grid_operator.atomic_write_bytes

    def interrupt_checkpoint(path: Path, content: bytes) -> None:
        if path == checkpoint:
            raise KeyboardInterrupt
        original(path, content)

    monkeypatch.setattr(grid_operator, "atomic_write_bytes", interrupt_checkpoint)
    with pytest.raises(KeyboardInterrupt):
        import_retrieved_results(local, incoming, SHARD)
    assert checkpoint.read_bytes() == before

    monkeypatch.setattr(grid_operator, "atomic_write_bytes", original)
    assert import_retrieved_results(local, incoming, SHARD).is_complete


def test_repeated_collection_preserves_already_committed_part_files(
    collection_runs: tuple[Path, Path, Path, SnapshotManifest],
) -> None:
    source, local, incoming, snapshot = collection_runs
    _process_collection_fixture(source, incoming, snapshot)
    assert import_retrieved_results(local, incoming, SHARD).is_complete
    parts = shard_paths(local, SHARD).parts
    before = {path.name: (path.stat().st_ino, path.read_bytes()) for path in parts.iterdir()}

    assert import_retrieved_results(local, incoming, SHARD).is_complete

    assert {
        path.name: (path.stat().st_ino, path.read_bytes()) for path in parts.iterdir()
    } == before


@pytest.mark.parametrize("fault", ["binding", "count", "missing_part", "symlink_part"])
def test_collection_refuses_corrupt_local_committed_history(
    collection_runs: tuple[Path, Path, Path, SnapshotManifest], fault: str, tmp_path: Path
) -> None:
    source, local, incoming, snapshot = collection_runs
    _process_collection_fixture(source, incoming, snapshot)
    assert import_retrieved_results(local, incoming, SHARD).is_complete
    paths = shard_paths(local, SHARD)
    if fault in {"binding", "count"}:
        payload = json.loads(paths.checkpoint.read_bytes())
        if fault == "binding":
            payload["model_config_fingerprint"] = "c" * 64
        else:
            payload["annotation_count"] += 1
        paths.checkpoint.write_text(json.dumps(payload), encoding="utf-8")
    else:
        part = next(paths.parts.iterdir())
        outside = tmp_path / "outside.parquet"
        part.rename(outside)
        if fault == "symlink_part":
            part.symlink_to(outside)

    before = paths.checkpoint.read_bytes()
    with pytest.raises(GridOperatorError, match="committed history"):
        import_retrieved_results(local, incoming, SHARD)
    assert paths.checkpoint.read_bytes() == before


@pytest.mark.parametrize("fault", ["snapshot", "checkpoint", "part", "symlink", "fifo"])
def test_collection_validates_its_private_copy_before_committing(
    collection_runs: tuple[Path, Path, Path, SnapshotManifest], fault: str, tmp_path: Path
) -> None:
    source, local, incoming, snapshot = collection_runs
    _process_collection_fixture(source, incoming, snapshot)
    paths = shard_paths(incoming, SHARD)
    if fault == "snapshot":
        (incoming / "snapshot.json").write_bytes(b"not JSON")
    elif fault == "checkpoint":
        paths.checkpoint.unlink()
    elif fault == "part":
        next(paths.parts.iterdir()).write_bytes(b"not parquet")
    elif fault == "symlink":
        (paths.root / "unexpected").symlink_to(tmp_path / "missing")
    else:
        os.mkfifo(paths.root / "unexpected")

    with pytest.raises(GridOperatorError):
        import_retrieved_results(local, incoming, SHARD)
    assert not shard_paths(local, SHARD).checkpoint.exists()
    assert not list(local.glob(".collection-*"))


def test_collection_refuses_to_import_its_own_run(
    collection_runs: tuple[Path, Path, Path, SnapshotManifest],
) -> None:
    source, local, _, snapshot = collection_runs
    _process_collection_fixture(source, local, snapshot)

    with pytest.raises(GridOperatorError, match="must be distinct"):
        import_retrieved_results(local, local, SHARD)


def test_staged_bytes_are_verified_before_a_job_can_run(
    portable_prepared: tuple[Path, Path, Path, SnapshotManifest],
) -> None:
    project, source, run, snapshot = portable_prepared
    prepared_bundle = prepare_portable_job(
        run,
        project,
        source,
        snapshot,
        SHARD,
        remote_bundle_dir="/scratch/lang-bundle",
    )
    staged_code = prepared_bundle.payload_root / "project" / "src" / "module.py"
    staged_code.write_text("VALUE = tampered\n", encoding="utf-8")

    with pytest.raises(GridOperatorError, match="staged file hash does not match"):
        verify_prepared_bundle(prepared_bundle.payload_root)


def test_transfer_and_retrieval_are_explicit_no_shell_argv_contracts(
    portable_prepared: tuple[Path, Path, Path, SnapshotManifest],
    tmp_path: Path,
) -> None:
    project, source, run, snapshot = portable_prepared
    prepared_bundle = prepare_portable_job(
        run,
        project,
        source,
        snapshot,
        SHARD,
        remote_bundle_dir="/scratch/lang-bundle",
    )

    send = build_bundle_transfer_argv(prepared_bundle, "/scratch/remote/lang-bundle")
    incoming = tmp_path / "incoming"
    receive = build_result_retrieval_argv("/scratch/remote/lang-bundle/run", incoming, SHARD)

    # The argv is the safety contract: pin it exactly rather than spot-checking
    # flags, so a dropped --checksum or a missing -- separator cannot pass.
    expected_destination = shard_paths(incoming, SHARD).root
    assert send == (
        "rsync",
        "--archive",
        "--checksum",
        "--protect-args",
        "--",
        f"{prepared_bundle.payload_root}/",
        "/scratch/remote/lang-bundle/",
    )
    assert receive == (
        "rsync",
        "--archive",
        "--checksum",
        "--protect-args",
        "--",
        f"/scratch/remote/lang-bundle/run/shards/{expected_destination.name}/",
        f"{expected_destination}/",
    )
    assert all("sh -c" not in item for item in (*send, *receive))


def test_submission_passes_through_the_caller_walltime_and_retry_choice(
    prepared: tuple[Path, Path, SnapshotManifest],
) -> None:
    """A non-default walltime and ``night_noretry=False`` must not be defaulted."""
    _, run, snapshot = prepared
    walltime = MAX_WALLTIME_SECONDS - 300
    bundle, paths = prepare_job(run, snapshot, SHARD, walltime_seconds=walltime, **REMOTE)
    observed: list[tuple[str, ...]] = []

    # Planning alone must already carry both choices into the argv it prints.
    planned, planned_result = submit_job(
        paths,
        bundle,
        policy=_verdict(),
        allowed_root=run,
        walltime_seconds=walltime,
        night_noretry=False,
    )
    assert planned_result is None
    assert planned.walltime_seconds == walltime
    assert "core=1,walltime=0:25:00" in planned.argv
    assert "night=noretry" not in planned.argv

    plan, result = submit_job(
        paths,
        bundle,
        policy=_verdict(),
        allowed_root=run,
        apply=True,
        walltime_seconds=walltime,
        night_noretry=False,
        runner=_runner(  # type: ignore[arg-type]
            [CommandResult((), 0, "OAR_JOB_ID=6917618" + chr(10), "")], observed
        ),
    )

    assert result is not None
    assert plan.walltime_seconds == walltime
    assert "core=1,walltime=0:25:00" in plan.argv
    assert "core=1,walltime=0:25:00" in observed[0]
    assert "night=noretry" not in plan.argv
    assert "night=noretry" not in observed[0]


def test_result_retrieval_binds_labels_and_the_exact_remote_source(tmp_path: Path) -> None:
    """Both inputs are refused under their own label, and the source is exact."""
    staging = tmp_path / "staging"

    with pytest.raises(GridOperatorError) as error:
        build_result_retrieval_argv("/scratch/a b", staging, SHARD)
    assert str(error.value) == "remote run directory must not contain shell metacharacters"

    with pytest.raises(GridOperatorError) as error:
        build_result_retrieval_argv("/scratch/run", staging, "region.txt")
    assert str(error.value) == "shard must be a Parquet path"

    key = shard_paths(staging, SHARD).root.name
    assert build_result_retrieval_argv("/scratch/run/", staging, SHARD)[-2] == (
        f"/scratch/run/shards/{key}/"
    )
    assert build_result_retrieval_argv("/scratch/runX", staging, SHARD)[-2] == (
        f"/scratch/runX/shards/{key}/"
    )
    assert build_result_retrieval_argv("/", staging, SHARD)[-2] == f"//shards/{key}/"


def test_planning_honours_explicit_limits_and_evaluation_time(
    prepared: tuple[Path, Path, SnapshotManifest],
) -> None:
    """A plan must use the caller's attempt limit, freshness demand, and clock."""
    _, run, snapshot = prepared
    bundle, paths = prepare_job(run, snapshot, SHARD, **REMOTE)
    captured_at = datetime(2026, 9, 9, 21, 0, tzinfo=UTC)
    policy = PolicyVerdict(
        PolicyDecision.ALLOWED,
        ("test verdict",),
        PolicyEvidence(0, False, True, (), captured_at=captured_at),
    )

    # The caller's own instant makes the evidence fresh; the real clock would not.
    fresh, _ = submit_job(
        paths,
        bundle,
        policy=policy,
        allowed_root=run,
        require_fresh_policy=True,
        now=captured_at,
    )
    assert fresh.may_apply

    # A stale instant must block precisely because freshness was demanded.
    stale, _ = submit_job(
        paths,
        bundle,
        policy=policy,
        allowed_root=run,
        require_fresh_policy=True,
        now=datetime(2026, 9, 10, 21, 0, tzinfo=UTC),
    )
    assert not stale.may_apply
    assert stale.blocked_reason is not None

    # The attempt limit is the caller's, and it is reached at the first attempt.
    _write_submitted_intent(paths, bundle)
    limited, _ = submit_job(
        paths,
        bundle,
        policy=policy,
        allowed_root=run,
        max_attempts=1,
        now=captured_at,
    )
    assert not limited.may_apply
    assert limited.blocked_reason is not None
    assert "maximum of 1 attempts" in limited.blocked_reason


def _write_submitted_intent(paths: JobPaths, bundle: JobBundle) -> None:
    """Record a terminal, acknowledged first attempt so a retry may be planned."""
    intent = SubmissionIntent(
        bundle_id=bundle.bundle_id,
        shard=bundle.shard,
        job_name=f"lang-{bundle.bundle_id[:16]}",
        walltime_seconds=MAX_WALLTIME_SECONDS,
        cores=1,
        recorded_at="2026-09-09T21:00:00+00:00",
        job_id=6917617,
        outcome="submitted",
        terminal_state="terminated",
        reconciled_at="2026-09-09T21:05:00+00:00",
        result_acknowledged=True,
    )
    paths.intent.write_text(json.dumps(intent.to_payload()), encoding="utf-8")


def test_an_applied_submission_records_a_utc_intent_and_requests_night_noretry(
    prepared: tuple[Path, Path, SnapshotManifest],
) -> None:
    """Without an injected clock the intent is still timezone-aware UTC."""
    _, run, snapshot = prepared
    bundle, paths = prepare_job(run, snapshot, SHARD, **REMOTE)
    observed: list[tuple[str, ...]] = []

    plan, result = submit_job(
        paths,
        bundle,
        policy=_verdict(),
        allowed_root=run,
        apply=True,
        runner=_runner(  # type: ignore[arg-type]
            [CommandResult((), 0, "OAR_JOB_ID=6917617\n", "")], observed
        ),
    )

    assert result is not None
    assert observed and "night=noretry" in observed[0]
    assert "night=noretry" in plan.argv
    recorded = read_intent(paths.intent)
    assert recorded.recorded_at.endswith("+00:00")
    assert datetime.fromisoformat(recorded.recorded_at).tzinfo is not None
    assert plan.walltime_seconds == MAX_WALLTIME_SECONDS


def test_bundle_transfer_binds_identity_label_and_exact_destination(
    portable_prepared: tuple[Path, Path, Path, SnapshotManifest],
) -> None:
    """The transfer argv is a safety contract, so pin each part of it.

    Only a verified ``PreparedJob`` may be transferred, the payload is checked
    against *this* bundle rather than whatever bundle it happens to contain,
    the remote path is refused under its own label, and the destination keeps
    exactly one trailing separator.
    """
    project, source, run, snapshot = portable_prepared
    prepared = prepare_portable_job(
        run, project, source, snapshot, SHARD, remote_bundle_dir="/scratch/lang-bundle"
    )

    with pytest.raises(GridOperatorError) as error:
        build_bundle_transfer_argv(object(), "/scratch/lang-bundle")  # type: ignore[arg-type]
    assert str(error.value) == "prepared bundle must be a PreparedJob"

    foreign = replace(prepared, bundle=replace(prepared.bundle, source_sha256="c" * 64))
    with pytest.raises(GridOperatorError):
        build_bundle_transfer_argv(foreign, "/scratch/lang-bundle")

    with pytest.raises(GridOperatorError) as error:
        build_bundle_transfer_argv(prepared, "/scratch/a b")
    assert str(error.value) == "remote bundle directory must not contain shell metacharacters"

    # Only "/" is stripped, and a root destination stays rooted.
    assert build_bundle_transfer_argv(prepared, "/scratch/bundle/")[-1] == "/scratch/bundle/"
    assert build_bundle_transfer_argv(prepared, "/scratch/bundleX")[-1] == "/scratch/bundleX/"
    assert build_bundle_transfer_argv(prepared, "/")[-1] == "//"


@pytest.mark.parametrize(
    ("overrides", "message"),
    [
        # Each message names the offending input, so an operator can tell which
        # path was refused; assert the full text, not just the failure kind.
        (
            {"remote_project_dir": "relative/path"},
            "remote project directory must be an absolute path",
        ),
        (
            {"remote_project_dir": ""},
            "remote project directory must be a non-empty string",
        ),
        (
            {"remote_project_dir": "/scratch/../evil"},
            "remote project directory must not contain traversal components",
        ),
        (
            {"remote_run_dir": "/scratch/a b"},
            "remote run directory must not contain shell metacharacters",
        ),
        (
            {"remote_run_dir": "relative/run"},
            "remote run directory must be an absolute path",
        ),
        (
            {"remote_source_dir": "/scratch/$(evil)"},
            "remote source directory must not contain shell metacharacters",
        ),
        ({"remote_source_dir": ""}, "remote source directory must be a non-empty string"),
        (
            {"remote_source_dir": "/scratch/../evil"},
            "remote source directory must not contain traversal components",
        ),
        (
            {"remote_bundle_dir": "/scratch/../bundle"},
            "remote bundle directory must not contain traversal components",
        ),
        (
            {"remote_bundle_dir": "/scratch/bundle;rm"},
            "remote bundle directory must not contain shell metacharacters",
        ),
        (
            {"glotlid_model_path": "models/model_v3.bin"},
            "remote GlotLID model path must be an absolute path",
        ),
        (
            {"glotlid_model_path": "/models/../model_v3.bin"},
            "remote GlotLID model path must not contain traversal components",
        ),
        ({"processing_seconds": 0}, "processing budget must be between"),
        ({"processing_seconds": MAX_PROCESSING_SECONDS + 1}, "processing budget must be between"),
        ({"batch_size": 0}, "batch size must be a positive integer"),
    ],
)
def test_the_job_script_rejects_unsafe_parameters(
    prepared: tuple[Path, Path, SnapshotManifest],
    overrides: dict[str, object],
    message: str,
) -> None:
    _, _, snapshot = prepared

    with pytest.raises(GridOperatorError) as error:
        render_job_script(bundle_for_shard(snapshot, SHARD), **{**REMOTE, **overrides})

    assert message in str(error.value)


def test_preparing_a_job_writes_an_executable_script_and_bundle(
    prepared: tuple[Path, Path, SnapshotManifest],
) -> None:
    _, run, snapshot = prepared

    bundle, paths = prepare_job(run, snapshot, SHARD, **REMOTE)

    assert paths.bundle.is_file()
    assert paths.script.is_file()
    assert stat.S_IMODE(paths.script.stat().st_mode) == 0o700
    assert read_bundle(paths.bundle) == bundle
    assert not paths.intent.exists()


def test_preparing_a_job_twice_is_idempotent(
    prepared: tuple[Path, Path, SnapshotManifest],
) -> None:
    _, run, snapshot = prepared

    first, _ = prepare_job(run, snapshot, SHARD, **REMOTE)
    second, _ = prepare_job(run, snapshot, SHARD, **REMOTE)

    assert first == second


def test_repreparing_a_bundle_rejects_changed_limits_or_remote_paths(
    prepared: tuple[Path, Path, SnapshotManifest],
) -> None:
    _, run, snapshot = prepared
    _, paths = prepare_job(run, snapshot, SHARD, **REMOTE)
    original_config = paths.config.read_bytes()
    original_script = paths.script.read_bytes()

    with pytest.raises(GridOperatorError, match="prepared job config is immutable"):
        prepare_job(
            run,
            snapshot,
            SHARD,
            **REMOTE,
            walltime_seconds=MAX_WALLTIME_SECONDS - 1,
        )
    with pytest.raises(GridOperatorError, match="prepared job config is immutable"):
        prepare_job(
            run,
            snapshot,
            SHARD,
            remote_project_dir="/home/other-project",
            remote_source_dir=REMOTE["remote_source_dir"],
            remote_run_dir=REMOTE["remote_run_dir"],
        )

    assert paths.config.read_bytes() == original_config
    assert paths.script.read_bytes() == original_script


def test_a_conflicting_bundle_in_a_job_directory_is_rejected(
    prepared: tuple[Path, Path, SnapshotManifest],
) -> None:
    _, run, snapshot = prepared
    bundle, paths = prepare_job(run, snapshot, SHARD, **REMOTE)
    payload = bundle.to_payload()
    payload["code_fingerprint"] = "c" * 64
    foreign = JobBundle(
        **{k: v for k, v in payload.items() if k not in {"bundle_id", "bundle_schema_version"}}
    )
    paths.bundle.write_text(json.dumps(foreign.to_payload()), encoding="utf-8")

    with pytest.raises(GridOperatorError, match="different bundle is already prepared"):
        prepare_job(run, snapshot, SHARD, **REMOTE)


def test_planning_contacts_no_scheduler_and_reports_the_argv(
    prepared: tuple[Path, Path, SnapshotManifest],
) -> None:
    _, run, snapshot = prepared
    bundle, paths = prepare_job(run, snapshot, SHARD, **REMOTE)

    plan = plan_submission(paths, bundle, policy=_verdict(), allowed_root=run)

    assert plan.may_apply
    assert plan.cores == 1
    assert plan.walltime_seconds == MAX_WALLTIME_SECONDS
    assert plan.argv[0] == "oarsub"
    assert "core=1,walltime=0:30:00" in plan.argv
    assert "night=noretry" in plan.argv
    assert not paths.intent.exists()
    assert json.loads(json.dumps(plan.to_payload()))["may_apply"] is True


def test_submit_job_defaults_are_fail_safe(
    prepared: tuple[Path, Path, SnapshotManifest],
) -> None:
    """Every default on the submission entry point must be the safe one.

    ``apply`` defaulting to ``True`` would submit a real job from a plain call,
    ``night_noretry`` defaulting to ``False`` would let a postponed night job
    be retried silently, and ``require_fresh_policy`` defaulting to ``True``
    would make planning demand live evidence it never gathered.
    """
    _, run, snapshot = prepared
    bundle, paths = prepare_job(run, snapshot, SHARD, **REMOTE)
    observed: list[tuple[str, ...]] = []
    unknown_freshness = PolicyVerdict(
        PolicyDecision.ALLOWED,
        ("test verdict",),
        PolicyEvidence(0, False, True, (), captured_at=None),
    )

    plan, result = submit_job(
        paths,
        bundle,
        policy=unknown_freshness,
        allowed_root=run,
        runner=_runner([], observed),  # type: ignore[arg-type]
    )

    assert result is None
    assert observed == []
    assert not paths.intent.exists()
    assert "night=noretry" in plan.argv
    assert plan.may_apply


def test_a_blocked_policy_prevents_applying(
    prepared: tuple[Path, Path, SnapshotManifest],
) -> None:
    _, run, snapshot = prepared
    bundle, paths = prepare_job(run, snapshot, SHARD, **REMOTE)

    for decision in (PolicyDecision.BLOCKED, PolicyDecision.UNKNOWN):
        plan = plan_submission(paths, bundle, policy=_verdict(decision), allowed_root=run)
        assert not plan.may_apply


def test_an_oversized_walltime_is_refused(
    prepared: tuple[Path, Path, SnapshotManifest],
) -> None:
    _, run, snapshot = prepared
    bundle, paths = prepare_job(run, snapshot, SHARD, **REMOTE)

    with pytest.raises(GridOperatorError, match="must not exceed 1800 seconds"):
        plan_submission(
            paths,
            bundle,
            policy=_verdict(),
            allowed_root=run,
            walltime_seconds=MAX_WALLTIME_SECONDS + 1,
        )


@pytest.mark.parametrize(
    "captured_at",
    [None, datetime(2026, 9, 5, 21, 0, tzinfo=UTC)],
)
def test_direct_apply_refuses_missing_or_stale_policy_evidence(
    prepared: tuple[Path, Path, SnapshotManifest], captured_at: datetime | None
) -> None:
    _, run, snapshot = prepared
    bundle, paths = prepare_job(run, snapshot, SHARD, **REMOTE)
    policy = PolicyVerdict(
        PolicyDecision.ALLOWED,
        ("test verdict",),
        PolicyEvidence(0, False, True, (), captured_at=captured_at),
    )
    observed: list[tuple[str, ...]] = []

    plan, result = submit_job(
        paths,
        bundle,
        policy=policy,
        allowed_root=run,
        apply=True,
        runner=_runner([], observed),  # type: ignore[arg-type]
        now=datetime(2026, 9, 5, 22, 0, tzinfo=UTC),
    )

    assert result is None
    assert not plan.may_apply
    assert plan.blocked_reason is not None
    assert any(word in plan.blocked_reason for word in ("fresh", "stale"))
    assert observed == []
    assert not paths.intent.exists()


def test_planning_rejects_a_walltime_different_from_prepared_job_config(
    prepared: tuple[Path, Path, SnapshotManifest],
) -> None:
    _, run, snapshot = prepared
    bundle, paths = prepare_job(
        run,
        snapshot,
        SHARD,
        **REMOTE,
        walltime_seconds=MAX_WALLTIME_SECONDS - 1,
    )

    with pytest.raises(GridOperatorError, match="differs from prepared job config"):
        plan_submission(paths, bundle, policy=_verdict(), allowed_root=run)


def test_without_the_apply_gate_nothing_is_submitted(
    prepared: tuple[Path, Path, SnapshotManifest],
) -> None:
    _, run, snapshot = prepared
    bundle, paths = prepare_job(run, snapshot, SHARD, **REMOTE)
    observed: list[tuple[str, ...]] = []

    plan, result = submit_job(
        paths,
        bundle,
        policy=_verdict(),
        allowed_root=run,
        runner=_runner([], observed),  # type: ignore[arg-type]
    )

    assert result is None
    assert plan.may_apply
    assert observed == []
    assert not paths.intent.exists()


def test_a_blocked_policy_is_not_submitted_even_with_the_apply_gate(
    prepared: tuple[Path, Path, SnapshotManifest],
) -> None:
    _, run, snapshot = prepared
    bundle, paths = prepare_job(run, snapshot, SHARD, **REMOTE)
    observed: list[tuple[str, ...]] = []

    _, result = submit_job(
        paths,
        bundle,
        policy=_verdict(PolicyDecision.BLOCKED),
        allowed_root=run,
        apply=True,
        runner=_runner([], observed),  # type: ignore[arg-type]
    )

    assert result is None
    assert observed == []
    assert not paths.intent.exists()


def test_the_intent_is_durable_before_oarsub_is_called(
    prepared: tuple[Path, Path, SnapshotManifest],
) -> None:
    _, run, snapshot = prepared
    bundle, paths = prepare_job(run, snapshot, SHARD, **REMOTE)
    seen_intent: list[bool] = []

    def _run(argv: Sequence[str], timeout: float) -> CommandResult:
        seen_intent.append(paths.intent.is_file())
        return CommandResult(tuple(argv), 0, "OAR_JOB_ID=1234\n", "")

    _, result = submit_job(
        paths,
        bundle,
        allowed_root=run,
        apply=True,
        runner=_run,
        policy=_verdict(captured_at=datetime(2026, 9, 5, 21, 59, tzinfo=GRID5000_TIMEZONE)),
        now=datetime(2026, 9, 5, 22, 0, tzinfo=GRID5000_TIMEZONE),
    )

    assert seen_intent == [True]
    assert result is not None
    assert result.outcome is SubmissionOutcome.SUBMITTED
    intent = read_intent(paths.intent)
    assert intent.job_id == 1234
    assert intent.outcome == "submitted"
    assert not intent.is_unresolved
    assert intent.recorded_at.startswith("2026-09-05T22:00")


def test_an_ambiguous_submission_leaves_an_unresolved_intent(
    prepared: tuple[Path, Path, SnapshotManifest],
) -> None:
    _, run, snapshot = prepared
    bundle, paths = prepare_job(run, snapshot, SHARD, **REMOTE)

    _, result = submit_job(
        paths,
        bundle,
        policy=_verdict(),
        allowed_root=run,
        apply=True,
        runner=_runner([CommandResult((), -1, "", "", True)]),  # type: ignore[arg-type]
    )

    assert result is not None
    assert result.outcome is SubmissionOutcome.AMBIGUOUS
    intent = read_intent(paths.intent)
    assert intent.job_id is None
    assert intent.is_unresolved


def test_an_unresolved_intent_blocks_any_further_submission(
    prepared: tuple[Path, Path, SnapshotManifest],
) -> None:
    _, run, snapshot = prepared
    bundle, paths = prepare_job(run, snapshot, SHARD, **REMOTE)
    submit_job(
        paths,
        bundle,
        policy=_verdict(),
        allowed_root=run,
        apply=True,
        runner=_runner([CommandResult((), -1, "", "", True)]),  # type: ignore[arg-type]
    )
    observed: list[tuple[str, ...]] = []

    plan, result = submit_job(
        paths,
        bundle,
        policy=_verdict(),
        allowed_root=run,
        apply=True,
        runner=_runner([], observed),  # type: ignore[arg-type]
    )

    assert result is None
    assert not plan.may_apply
    assert plan.blocked_reason is not None
    assert "unresolved intent" in plan.blocked_reason
    assert observed == []


def test_only_a_terminal_reconciled_and_acknowledged_paused_shard_can_retry(
    prepared: tuple[Path, Path, SnapshotManifest],
) -> None:
    source, run, snapshot = prepared
    bundle, paths = prepare_job(run, snapshot, SHARD, **REMOTE)
    submit_job(
        paths,
        bundle,
        policy=_verdict(),
        allowed_root=run,
        apply=True,
        runner=_runner([CommandResult((), 0, "OAR_JOB_ID=21\n", "")]),  # type: ignore[arg-type]
    )

    reconciled = reconcile_job(
        paths,
        apply=True,
        runner=_runner([CommandResult((), 0, "21: Terminated\n", "")]),  # type: ignore[arg-type]
    )
    assert reconciled.state is JobState.TERMINATED
    assert read_intent(paths.intent).terminal_state == "terminated"

    process_shard(
        run,
        source,
        SHARD,
        detector=lambda text: LanguageResult(
            "eng", 0.9, 0.1, 0.8, LanguageStatus.DETECTED, "detected"
        ),
        snapshot=snapshot,
        batch_size=2,
    )
    report = collect_results(run, SHARD)
    assert report.is_complete

    acknowledgment = acknowledge_collected_results(paths, report)
    assert acknowledgment.result_complete
    blocked = plan_submission(paths, bundle, policy=_verdict(), allowed_root=run)
    assert not blocked.may_apply
    assert blocked.blocked_reason is not None
    assert "complete results" in blocked.blocked_reason


def test_a_paused_result_acknowledgment_opens_one_bounded_same_shard_attempt(
    prepared: tuple[Path, Path, SnapshotManifest],
) -> None:
    source, run, snapshot = prepared
    bundle, paths = prepare_job(run, snapshot, SHARD, **REMOTE)
    submit_job(
        paths,
        bundle,
        policy=_verdict(),
        allowed_root=run,
        apply=True,
        runner=_runner([CommandResult((), 0, "OAR_JOB_ID=31\n", "")]),  # type: ignore[arg-type]
    )
    reconcile_job(
        paths,
        apply=True,
        runner=_runner([CommandResult((), 0, "31: Terminated\n", "")]),  # type: ignore[arg-type]
    )

    # A valid paused checkpoint with no committed rows is a real resumable state.
    process_shard(
        run,
        source,
        SHARD,
        detector=lambda text: LanguageResult(
            "eng", 0.9, 0.1, 0.8, LanguageStatus.DETECTED, "detected"
        ),
        snapshot=snapshot,
        batch_size=2,
        budget=ProcessingBudget(0.000001),
    )
    report = collect_results(run, SHARD)
    assert not report.is_complete
    assert not report.issues
    acknowledge_collected_results(paths, report)

    retry_plan = plan_submission(paths, bundle, policy=_verdict(), allowed_root=run)
    assert retry_plan.may_apply
    assert retry_plan.attempt == 2

    _, retry_result = submit_job(
        paths,
        bundle,
        policy=_verdict(),
        allowed_root=run,
        apply=True,
        runner=_runner([CommandResult((), 0, "OAR_JOB_ID=32\n", "")]),  # type: ignore[arg-type]
    )
    assert retry_result is not None
    reconcile_job(
        paths,
        apply=True,
        runner=_runner([CommandResult((), 0, "32: Terminated\n", "")]),  # type: ignore[arg-type]
    )
    acknowledge_collected_results(paths, report)

    exhausted = plan_submission(
        paths,
        bundle,
        policy=_verdict(),
        allowed_root=run,
        max_attempts=2,
    )
    assert not exhausted.may_apply
    assert exhausted.blocked_reason is not None
    assert "maximum" in exhausted.blocked_reason

    continuation = plan_submission(paths, bundle, policy=_verdict(), allowed_root=run)
    assert continuation.may_apply
    assert continuation.attempt == 3


def test_collected_acknowledgment_requires_terminal_reconciliation(
    prepared: tuple[Path, Path, SnapshotManifest],
) -> None:
    source, run, snapshot = prepared
    bundle, paths = prepare_job(run, snapshot, SHARD, **REMOTE)
    submit_job(
        paths,
        bundle,
        policy=_verdict(),
        allowed_root=run,
        apply=True,
        runner=_runner([CommandResult((), 0, "OAR_JOB_ID=30\n", "")]),  # type: ignore[arg-type]
    )
    process_shard(
        run,
        source,
        SHARD,
        detector=lambda text: LanguageResult(
            "eng", 0.9, 0.1, 0.8, LanguageStatus.DETECTED, "detected"
        ),
        snapshot=snapshot,
        batch_size=2,
        budget=ProcessingBudget(0.000001),
    )

    with pytest.raises(GridOperatorError, match="terminal scheduler state"):
        acknowledge_collected_results(paths, collect_results(run, SHARD))


def test_collection_rejects_a_complete_checkpoint_without_contiguous_parts(
    prepared: tuple[Path, Path, SnapshotManifest],
) -> None:
    _, run, snapshot = prepared
    paths = shard_paths(run, SHARD)
    checkpoint = ShardCheckpoint(
        snapshot_id=snapshot.snapshot_id,
        model_config_fingerprint=snapshot.model_config_fingerprint,
        shard=SHARD,
        batch_size=2,
        input_row_count=4,
        input_cursor=0,
        annotation_count=0,
        completed_parts=(),
        status=ShardStatus.PAUSED,
    )
    write_checkpoint(paths.checkpoint, checkpoint)
    payload = json.loads(paths.checkpoint.read_text(encoding="utf-8"))
    paths.checkpoint.write_text(
        json.dumps({**payload, "input_cursor": 4, "status": "complete"}), encoding="utf-8"
    )

    report = collect_results(run, SHARD)

    assert not report.is_complete
    assert any("checkpoint parts do not cover" in issue for issue in report.shards[0].issues)


def test_run_wide_active_intent_blocks_a_different_shard(tmp_path: Path) -> None:
    run, snapshot, roots = _two_shard_prepared(tmp_path)
    first, first_paths = prepare_job(run, snapshot, SHARD, **REMOTE)
    second, second_paths = prepare_job(run, snapshot, "other.parquet", **REMOTE)
    submit_job(
        first_paths,
        first,
        policy=_verdict(),
        allowed_root=run,
        apply=True,
        runner=_runner([CommandResult((), 0, "OAR_JOB_ID=41\n", "")]),  # type: ignore[arg-type]
    )

    plan = plan_submission(second_paths, second, policy=_verdict(), allowed_root=run)

    assert not plan.may_apply
    assert plan.blocked_reason is not None
    assert "another shard" in plan.blocked_reason
    assert roots["source"].is_dir()


def test_submission_lock_serializes_the_scheduler_call(tmp_path: Path) -> None:
    run, snapshot, _ = _two_shard_prepared(tmp_path)
    first, first_paths = prepare_job(run, snapshot, SHARD, **REMOTE)
    second, second_paths = prepare_job(run, snapshot, "other.parquet", **REMOTE)
    observed: list[tuple[str, ...]] = []

    def runner(argv: Sequence[str], timeout: float) -> CommandResult:
        with pytest.raises(GridOperatorError, match="submission lock"):
            submit_job(
                second_paths,
                second,
                policy=_verdict(),
                allowed_root=run,
                apply=True,
                runner=_runner([], observed),  # type: ignore[arg-type]
            )
        return CommandResult(tuple(argv), 0, "OAR_JOB_ID=42\n", "")

    _, result = submit_job(
        first_paths,
        first,
        policy=_verdict(),
        allowed_root=run,
        apply=True,
        runner=runner,
    )

    assert result is not None
    assert result.job_id == 42
    assert observed == []


def test_an_existing_job_blocks_a_second_submission(
    prepared: tuple[Path, Path, SnapshotManifest],
) -> None:
    _, run, snapshot = prepared
    bundle, paths = prepare_job(run, snapshot, SHARD, **REMOTE)
    submit_job(
        paths,
        bundle,
        policy=_verdict(),
        allowed_root=run,
        apply=True,
        runner=_runner([CommandResult((), 0, "OAR_JOB_ID=7\n", "")]),  # type: ignore[arg-type]
    )
    observed: list[tuple[str, ...]] = []

    plan, result = submit_job(
        paths,
        bundle,
        policy=_verdict(),
        allowed_root=run,
        apply=True,
        runner=_runner([], observed),  # type: ignore[arg-type]
    )

    assert result is None
    assert plan.blocked_reason is not None
    assert "job 7 was already submitted" in plan.blocked_reason
    assert observed == []


def test_a_rejected_submission_does_not_block_a_later_attempt(
    prepared: tuple[Path, Path, SnapshotManifest],
) -> None:
    _, run, snapshot = prepared
    bundle, paths = prepare_job(run, snapshot, SHARD, **REMOTE)
    submit_job(
        paths,
        bundle,
        policy=_verdict(),
        allowed_root=run,
        apply=True,
        runner=_runner([CommandResult((), 2, "", "refused")]),  # type: ignore[arg-type]
    )

    plan, result = submit_job(
        paths,
        bundle,
        policy=_verdict(),
        allowed_root=run,
        apply=True,
        runner=_runner([CommandResult((), 0, "OAR_JOB_ID=8\n", "")]),  # type: ignore[arg-type]
    )

    assert plan.blocked_reason is None
    assert result is not None
    assert result.job_id == 8


def test_rejected_submissions_still_count_toward_an_explicit_attempt_limit(
    prepared: tuple[Path, Path, SnapshotManifest],
) -> None:
    _, run, snapshot = prepared
    bundle, paths = prepare_job(run, snapshot, SHARD, **REMOTE)
    calls: list[tuple[str, ...]] = []

    def reject(argv: Sequence[str], timeout: float) -> CommandResult:
        calls.append(tuple(argv))
        return CommandResult(tuple(argv), 2, "", "refused")

    for _ in range(2):
        plan, result = submit_job(
            paths,
            bundle,
            policy=_verdict(),
            allowed_root=run,
            apply=True,
            runner=reject,
            max_attempts=1,
        )

    assert len(calls) == 1
    assert result is None
    assert plan.blocked_reason == "maximum of 1 attempts has been reached for this shard"
    assert read_intent(paths.intent).attempt == 1


def test_reconciliation_reports_a_missing_intent(
    prepared: tuple[Path, Path, SnapshotManifest],
) -> None:
    _, run, snapshot = prepared
    bundle = bundle_for_shard(snapshot, SHARD)

    reconciliation = reconcile_job(job_paths(run, bundle))

    assert reconciliation.intent is None
    assert not reconciliation.needs_operator_attention
    assert "no submission intent" in reconciliation.detail


def test_reconciliation_flags_an_unresolved_intent_for_an_operator(
    prepared: tuple[Path, Path, SnapshotManifest],
) -> None:
    _, run, snapshot = prepared
    bundle, paths = prepare_job(run, snapshot, SHARD, **REMOTE)
    submit_job(
        paths,
        bundle,
        policy=_verdict(),
        allowed_root=run,
        apply=True,
        runner=_runner([CommandResult((), -1, "", "", True)]),  # type: ignore[arg-type]
    )

    reconciliation = reconcile_job(paths)

    assert reconciliation.needs_operator_attention
    assert "query the scheduler by job name" in reconciliation.detail
    assert json.loads(json.dumps(reconciliation.to_payload()))["state"] == "unknown"


def test_reconciliation_resolver_binds_a_job_name_before_querying_state(
    prepared: tuple[Path, Path, SnapshotManifest],
) -> None:
    _, run, snapshot = prepared
    bundle, paths = prepare_job(run, snapshot, SHARD, **REMOTE)
    submit_job(
        paths,
        bundle,
        policy=_verdict(),
        allowed_root=run,
        apply=True,
        runner=_runner([CommandResult((), -1, "", "", True)]),  # type: ignore[arg-type]
    )

    reconciliation = reconcile_job(
        paths,
        apply=True,
        resolve_job_name=lambda name: 77,
        runner=_runner([CommandResult((), 0, "77: Terminated\n", "")]),  # type: ignore[arg-type]
    )

    assert reconciliation.state is JobState.TERMINATED
    assert read_intent(paths.intent).job_id == 77
    assert read_intent(paths.intent).terminal_state == "terminated"


def test_reconciliation_rejects_a_non_callable_job_name_resolver(
    prepared: tuple[Path, Path, SnapshotManifest],
) -> None:
    _, run, snapshot = prepared
    bundle, paths = prepare_job(run, snapshot, SHARD, **REMOTE)
    submit_job(
        paths,
        bundle,
        policy=_verdict(),
        allowed_root=run,
        apply=True,
        runner=_runner([CommandResult((), -1, "", "", True)]),  # type: ignore[arg-type]
    )

    with pytest.raises(GridOperatorError, match="resolver must be callable"):
        reconcile_job(  # type: ignore[arg-type]
            paths,
            apply=True,
            resolve_job_name=object(),
        )


def test_policy_evidence_gatherer_includes_account_wide_scheduler_output() -> None:
    observed: list[tuple[str, ...]] = []
    runner = _runner(
        [
            CommandResult((), 0, "policy-json", ""),
            CommandResult((), 0, "quota-text", ""),
            CommandResult((), 0, '{"123":{"state":"Running"}}', ""),
        ],
        observed,
    )

    evidence = gather_policy_evidence("nancy", runner=runner)  # type: ignore[arg-type]

    assert evidence == ("policy-json", "quota-text", '{"123":{"state":"Running"}}')
    assert observed[-1] == ("oarstat", "-u", "-J")


def test_production_job_name_resolver_uses_account_json_without_resubmitting() -> None:
    observed: list[tuple[str, ...]] = []

    def runner(argv: Sequence[str], timeout: float) -> CommandResult:
        observed.append(tuple(argv))
        return CommandResult(
            tuple(argv),
            0,
            '{"123":{"name":"lang-target","state":"Terminated"}}',
            "",
        )

    assert resolve_job_name("lang-target", runner=runner) == 123
    assert observed == [("oarstat", "-u", "-J")]


def test_reconciliation_queries_the_scheduler_only_behind_the_apply_gate(
    prepared: tuple[Path, Path, SnapshotManifest],
) -> None:
    _, run, snapshot = prepared
    bundle, paths = prepare_job(run, snapshot, SHARD, **REMOTE)
    submit_job(
        paths,
        bundle,
        policy=_verdict(),
        allowed_root=run,
        apply=True,
        runner=_runner([CommandResult((), 0, "OAR_JOB_ID=11\n", "")]),  # type: ignore[arg-type]
    )
    observed: list[tuple[str, ...]] = []

    dry = reconcile_job(paths, runner=_runner([], observed))  # type: ignore[arg-type]
    assert observed == []
    assert "pass the apply gate" in dry.detail

    live = reconcile_job(
        paths,
        apply=True,
        runner=_runner([CommandResult((), 0, "11: Running\n", "")], observed),  # type: ignore[arg-type]
    )

    assert observed == [("oarstat", "-j", "11", "-s")]
    assert live.state is JobState.ACTIVE
    assert not live.needs_operator_attention


def test_a_terminated_job_needs_no_further_attention(
    prepared: tuple[Path, Path, SnapshotManifest],
) -> None:
    _, run, snapshot = prepared
    bundle, paths = prepare_job(run, snapshot, SHARD, **REMOTE)
    submit_job(
        paths,
        bundle,
        policy=_verdict(),
        allowed_root=run,
        apply=True,
        runner=_runner([CommandResult((), 0, "OAR_JOB_ID=12\n", "")]),  # type: ignore[arg-type]
    )

    reconciliation = reconcile_job(
        paths,
        apply=True,
        runner=_runner([CommandResult((), 0, "12: Terminated\n", "")]),  # type: ignore[arg-type]
    )

    assert reconciliation.state is JobState.TERMINATED
    assert not reconciliation.needs_operator_attention


def test_an_unreadable_intent_is_reported(
    prepared: tuple[Path, Path, SnapshotManifest],
) -> None:
    _, run, snapshot = prepared
    _, paths = prepare_job(run, snapshot, SHARD, **REMOTE)
    paths.intent.write_text("{not-json", encoding="utf-8")

    with pytest.raises(GridOperatorError, match="cannot read intent"):
        reconcile_job(paths)


@pytest.mark.parametrize(
    ("mutation", "message"),
    [
        ({"intent_schema_version": 9}, "unsupported intent schema version"),
        ({"job_id": "7"}, "job_id must be an integer or null"),
        ({"outcome": 7}, "outcome must be a string or null"),
        ({"detail": []}, "detail must be a string or null"),
        ({"shard": None}, "must be a string"),
    ],
)
def test_a_malformed_intent_payload_is_rejected(
    prepared: tuple[Path, Path, SnapshotManifest],
    mutation: dict[str, object],
    message: str,
) -> None:
    _, run, snapshot = prepared
    bundle, paths = prepare_job(run, snapshot, SHARD, **REMOTE)
    submit_job(
        paths,
        bundle,
        policy=_verdict(),
        allowed_root=run,
        apply=True,
        runner=_runner([CommandResult((), 0, "OAR_JOB_ID=13\n", "")]),  # type: ignore[arg-type]
    )
    payload = json.loads(paths.intent.read_text(encoding="utf-8"))
    paths.intent.write_text(json.dumps({**payload, **mutation}), encoding="utf-8")

    with pytest.raises(GridOperatorError, match=message):
        read_intent(paths.intent)


def test_collecting_results_validates_what_the_job_produced(
    prepared: tuple[Path, Path, SnapshotManifest],
) -> None:
    source, run, snapshot = prepared

    incomplete = collect_results(run, SHARD)
    assert not incomplete.is_complete

    process_shard(
        run,
        source,
        SHARD,
        detector=lambda text: LanguageResult(
            "eng", 0.9, 0.1, 0.8, LanguageStatus.DETECTED, "detected"
        ),
        snapshot=snapshot,
        batch_size=2,
    )

    report = collect_results(run, SHARD)

    assert report.is_complete
    assert report.annotation_count == 4


@pytest.mark.parametrize("site", ["", "nancy;rm", "../nancy", 7])
def test_gathering_policy_evidence_rejects_an_unsafe_site(site: object) -> None:
    from osm_polygon_description_tag.workflow.grid_operator import gather_policy_outputs

    with pytest.raises(GridOperatorError, match="site must be a simple alphanumeric name"):
        gather_policy_outputs(site)  # type: ignore[arg-type]


def test_gathering_policy_evidence_captures_both_commands() -> None:
    from osm_polygon_description_tag.workflow.grid_operator import gather_policy_outputs

    observed: list[tuple[str, ...]] = []
    runner = _runner(
        [CommandResult((), 0, "policy-json", ""), CommandResult((), 0, "quota-text", "")], observed
    )

    usage, quota = gather_policy_outputs("nancy", runner=runner)  # type: ignore[arg-type]

    assert usage == "policy-json"
    assert quota == "quota-text"
    assert observed == [
        ("usagepolicycheck", "-t", "--sites", "nancy", "--json"),
        ("quota", "-p", "-w"),
    ]


def test_evidence_that_cannot_be_captured_is_unknown_rather_than_clear() -> None:
    from osm_polygon_description_tag.workflow.grid_operator import gather_policy_outputs

    failing = _runner([CommandResult((), 1, "", "nope"), CommandResult((), -1, "", "", True)])

    assert gather_policy_outputs("nancy", runner=failing) == (None, None)  # type: ignore[arg-type]


def test_a_missing_preflight_executable_is_unknown_rather_than_clear() -> None:
    from osm_polygon_description_tag.workflow.grid_operator import gather_policy_outputs
    from osm_polygon_description_tag.workflow.grid_scheduler import SchedulerError

    def _absent(argv: Sequence[str], timeout: float) -> CommandResult:
        raise SchedulerError(f"scheduler executable is not available: {argv[0]}")

    assert gather_policy_outputs("nancy", runner=_absent) == (None, None)


def test_cascade_job_script_passes_the_pinned_glotlid_path(
    prepared: tuple[Path, Path, SnapshotManifest],
    tmp_path: Path,
) -> None:
    source, _, original = prepared
    run = tmp_path / "cascade-run"
    snapshot = prepare_snapshot(
        source,
        run,
        code_fingerprint=original.code_fingerprint,
        lock_fingerprint=original.lock_fingerprint,
        model_identity=cascade_model_identity(original.model_identity.policy),
    )

    _, paths = prepare_job(
        run,
        snapshot,
        SHARD,
        **REMOTE,
        glotlid_model_path="/home/user/models/glotlid-v3/model_v3.bin",
    )
    script = paths.script.read_text(encoding="utf-8")

    assert 'export PATH="${PATH}:$HOME/.local/bin"' in script
    assert "--glotlid-model-path /home/user/models/glotlid-v3/model_v3.bin" in script


def test_cascade_job_requires_a_pinned_glotlid_path(
    prepared: tuple[Path, Path, SnapshotManifest],
    tmp_path: Path,
) -> None:
    source, _, original = prepared
    run = tmp_path / "cascade-run"
    snapshot = prepare_snapshot(
        source,
        run,
        code_fingerprint=original.code_fingerprint,
        lock_fingerprint=original.lock_fingerprint,
        model_identity=cascade_model_identity(original.model_identity.policy),
    )

    with pytest.raises(GridOperatorError, match="requires a GlotLID model path"):
        prepare_job(run, snapshot, SHARD, **REMOTE)
