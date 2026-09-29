"""Preparing, gating, submitting, reconciling, and collecting one Grid'5000 job."""

import json
import os
import shutil
import subprocess
import sys
from collections.abc import Sequence
from datetime import UTC, datetime
from pathlib import Path

import matplotlib
import pytest
from shapely.geometry import Polygon

from osm_polygon_description_tag.dataset.languages.checkpoint import (
    WorkerBusyError,
    exclusive_worker_lock,
    read_checkpoint,
    shard_paths,
)
from osm_polygon_description_tag.dataset.languages.models import (
    LanguageResult,
    LanguageStatus,
)
from osm_polygon_description_tag.dataset.languages.snapshot import (
    SnapshotManifest,
    fingerprint_lockfile,
    fingerprint_project_source,
    prepare_snapshot,
)
from osm_polygon_description_tag.dataset.languages.worker import ProcessingBudget, process_shard
from osm_polygon_description_tag.dataset.storage import write_geoparquet
from osm_polygon_description_tag.workflow import grid_operator
from osm_polygon_description_tag.workflow.grid_operator import (
    GridOperatorError,
    JobBundle,
    JobPaths,
    SubmissionIntent,
    build_bundle_transfer_argv,
    build_result_retrieval_argv,
    bundle_for_shard,
    collect_results,
    import_retrieved_results,
    prepare_job,
    prepare_portable_job,
    read_bundle,
    render_job_script,
    submit_job,
    verify_prepared_bundle,
)
from osm_polygon_description_tag.workflow.grid_policy import (
    MAX_PROCESSING_SECONDS,
    MAX_WALLTIME_SECONDS,
    PolicyDecision,
    PolicyEvidence,
    PolicyVerdict,
)
from osm_polygon_description_tag.workflow.grid_scheduler import (
    CommandResult,
)
from tests.conftest import make_record_dict
from tests.helpers.messages import exactly
from tests.helpers.sentences import REMOTE_SAT_MODEL_PATH, fake_splitter

SHARD = "region.parquet"


REMOTE = {
    "remote_project_dir": "/home/user/project",
    "remote_source_dir": "/scratch/staging/source",
    "remote_run_dir": "/scratch/staging/run",
    "sat_model_path": "/home/user/models/sat-3l-sm/model.safetensors",
}


def _verdict(
    decision: PolicyDecision = PolicyDecision.ALLOWED, *, captured_at: datetime | None = None
) -> PolicyVerdict:
    return PolicyVerdict(
        decision,
        ("test verdict",),
        PolicyEvidence(0, False, True, (), captured_at=captured_at or datetime.now(UTC)),
    )


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

    with pytest.raises(
        GridOperatorError, match=exactly("bundle id does not match its canonical content")
    ):
        JobBundle.from_payload({**payload, "input_row_count": 99})
    with pytest.raises(GridOperatorError, match=exactly("unsupported bundle schema version")):
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


@pytest.fixture
def shell_safe_tmp(tmp_path: Path) -> Path:
    """Skip when the temp path cannot serve as a remote directory.

    A remote path may not contain shell metacharacters, because OAR evaluates
    the stored command through a shell. Temp directories on a mounted volume
    routinely contain a space, which is exactly what that rule forbids, so the
    end-to-end job-script runs below need a plain path to stand in for one.
    """
    offending = [
        character for character in grid_operator._SHELL_METACHARACTERS if character in str(tmp_path)
    ]
    if offending:
        pytest.skip(
            "remote directories may not contain shell metacharacters "
            f"(found {offending!r} in the temp path); point TMPDIR at a plain "
            "path to run the end-to-end job-script tests"
        )
    return tmp_path


def test_bare_prepared_job_script_runs_without_a_portable_payload(
    prepared: tuple[Path, Path, SnapshotManifest],
    tmp_path: Path,
    shell_safe_tmp: Path,
) -> None:
    _, run, snapshot = prepared
    bundle, paths = prepare_job(
        run,
        snapshot,
        SHARD,
        remote_project_dir=str(tmp_path / "project"),
        remote_source_dir=str(tmp_path / "source"),
        remote_run_dir=str(tmp_path / "run-output"),
        sat_model_path=REMOTE_SAT_MODEL_PATH,
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
        sat_model_path=REMOTE_SAT_MODEL_PATH,
    )

    assert "cd /scratch/project-safe" in script
    assert "--source-root /scratch/source-safe" in script
    assert "--run-dir /scratch/run-safe" in script
    assert "--shard region.parquet" in script
    assert "verify_prepared_bundle" in script

    with pytest.raises(
        GridOperatorError, match=exactly("processing budget must be less than walltime")
    ):
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
        splitter=fake_splitter(),
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
        sat_model_path=REMOTE_SAT_MODEL_PATH,
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
    shell_safe_tmp: Path,
) -> None:
    project, source, run, snapshot = portable_prepared
    prepared = prepare_portable_job(
        run,
        project,
        source,
        snapshot,
        SHARD,
        remote_bundle_dir=str(tmp_path / "remote-bundle"),
        sat_model_path=REMOTE_SAT_MODEL_PATH,
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
        sat_model_path=REMOTE_SAT_MODEL_PATH,
    )
    process_shard(
        run,
        source,
        SHARD,
        detector=lambda text: LanguageResult(
            "eng", 0.9, 0.1, 0.8, LanguageStatus.DETECTED, "detected"
        ),
        splitter=fake_splitter(),
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
        sat_model_path=REMOTE_SAT_MODEL_PATH,
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
        splitter=fake_splitter(),
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
        sat_model_path=REMOTE_SAT_MODEL_PATH,
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
        splitter=fake_splitter(),
        snapshot=snapshot,
        batch_size=2,
        budget=budget,
    )


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

    with pytest.raises(
        GridOperatorError, match=exactly("imported results failed local collection validation")
    ):
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

    with pytest.raises(
        GridOperatorError, match=exactly("retrieved checkpoint would regress local progress")
    ):
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

    with pytest.raises(
        GridOperatorError, match=exactly("retrieved and local shard directories must be distinct")
    ):
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
        sat_model_path=REMOTE_SAT_MODEL_PATH,
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
        sat_model_path=REMOTE_SAT_MODEL_PATH,
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
