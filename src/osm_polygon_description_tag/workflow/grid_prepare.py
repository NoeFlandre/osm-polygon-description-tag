"""Prepare the immutable local job bundle, config, and script for one shard."""

from pathlib import Path

from osm_polygon_description_tag.dataset.languages.payloads import require_object
from osm_polygon_description_tag.dataset.languages.snapshot import (
    SnapshotManifest,
)
from osm_polygon_description_tag.runtime.atomic import (
    atomic_write_bytes,
    atomic_write_json,
)
from osm_polygon_description_tag.workflow.grid_job_script import (
    _glotlid_model_path_for_snapshot,
    _validate_job_limits,
    render_job_script,
)
from osm_polygon_description_tag.workflow.grid_models import (
    JOB_CONFIG_SCHEMA_VERSION,
    GridOperatorError,
    JobBundle,
    JobPaths,
    _read_json,
    bundle_for_shard,
    job_paths,
    read_bundle,
)
from osm_polygon_description_tag.workflow.grid_policy import (
    MAX_PROCESSING_SECONDS,
    MAX_WALLTIME_SECONDS,
    REQUIRED_CORES,
)
from osm_polygon_description_tag.workflow.grid_state import (
    initialize_shard_checkpoint,
)


def prepare_job(
    run_dir: Path,
    snapshot: SnapshotManifest,
    shard: str,
    *,
    remote_project_dir: str,
    remote_source_dir: str,
    remote_run_dir: str,
    processing_seconds: int = MAX_PROCESSING_SECONDS,
    batch_size: int = 512,
    walltime_seconds: int = MAX_WALLTIME_SECONDS,
    remote_bundle_dir: str | None = None,
    sat_model_path: str,
    glotlid_model_path: str | None = None,
    submission_locked: bool = False,
) -> tuple[JobBundle, JobPaths]:
    """Write the immutable bundle, job script, and initial checkpoint of a shard.

    Pass ``submission_locked`` when the caller already holds the run-wide
    submission lock; the worker lock is then the only lock taken here.
    """
    bundle = bundle_for_shard(snapshot, shard)
    paths = job_paths(run_dir, bundle)
    existing = _existing_bundle(paths)
    if existing is not None and existing != bundle:
        raise GridOperatorError("a different bundle is already prepared in this job directory")
    remote_model_path = _glotlid_model_path_for_snapshot(snapshot, glotlid_model_path)
    script = render_job_script(
        bundle,
        remote_project_dir=remote_project_dir,
        remote_source_dir=remote_source_dir,
        remote_run_dir=remote_run_dir,
        processing_seconds=processing_seconds,
        batch_size=batch_size,
        walltime_seconds=walltime_seconds,
        remote_bundle_dir=remote_bundle_dir,
        sat_model_path=sat_model_path,
        glotlid_model_path=remote_model_path,
    )
    config = _job_config_payload(
        bundle,
        processing_seconds=processing_seconds,
        batch_size=batch_size,
        walltime_seconds=walltime_seconds,
    )
    script_bytes = script.encode("utf-8")  # pragma: no mutate - codec alias only
    if existing is None:
        _write_prepared_artifacts(paths, bundle, config, script_bytes)
    else:
        _verify_immutable_prepared_artifacts(paths, config, script_bytes)
    initialize_shard_checkpoint(
        run_dir, bundle, batch_size=batch_size, submission_locked=submission_locked
    )
    return bundle, paths


def _write_prepared_artifacts(
    paths: JobPaths, bundle: JobBundle, config: dict[str, object], script_bytes: bytes
) -> None:
    atomic_write_json(paths.bundle, bundle.to_payload())
    atomic_write_json(paths.config, config)
    atomic_write_bytes(paths.script, script_bytes)
    paths.script.chmod(0o700)


def _verify_immutable_prepared_artifacts(
    paths: JobPaths, config: dict[str, object], script_bytes: bytes
) -> None:
    _verify_immutable_config(paths.config, config)
    _verify_immutable_script(paths.script, script_bytes)


def _verify_immutable_config(path: Path, expected: dict[str, object]) -> None:
    _require_immutable_file(path, "prepared job config is immutable and must be present")
    if _read_json(path, "job config") != expected:
        raise GridOperatorError("prepared job config is immutable; restage with a new job bundle")


def _verify_immutable_script(path: Path, expected: bytes) -> None:
    _require_immutable_file(path, "prepared job config is immutable and its script must be present")
    if path.read_bytes() != expected:
        raise GridOperatorError("prepared job config is immutable; restage with a new job bundle")


def _require_immutable_file(path: Path, message: str) -> None:
    if path.is_symlink() or not path.is_file():
        raise GridOperatorError(message)


def _job_config_payload(
    bundle: JobBundle, *, processing_seconds: int, batch_size: int, walltime_seconds: int
) -> dict[str, object]:
    # ``prepare_job`` renders the job script from these same three values first, and
    # ``render_job_script`` validates them; checking them again here would only repeat
    # that call with the same arguments.
    return {
        "job_config_schema_version": JOB_CONFIG_SCHEMA_VERSION,
        "bundle_id": bundle.bundle_id,
        "processing_seconds": processing_seconds,
        "batch_size": batch_size,
        "walltime_seconds": walltime_seconds,
        "cores": REQUIRED_CORES,
        "thread_limit": 1,
    }


def _read_job_config(path: Path, bundle: JobBundle) -> dict[str, int]:
    reader = require_object(
        _read_json(path, "job config"), error=GridOperatorError, label="job config"
    )
    if reader.integer("job_config_schema_version") != JOB_CONFIG_SCHEMA_VERSION:
        raise GridOperatorError("unsupported job config schema version")
    if reader.text("bundle_id") != bundle.bundle_id:
        raise GridOperatorError("job config bundle id does not match the prepared bundle")
    values = {
        "processing_seconds": reader.integer("processing_seconds"),
        "batch_size": reader.integer("batch_size"),
        "walltime_seconds": reader.integer("walltime_seconds"),
        "cores": reader.integer("cores"),
        "thread_limit": reader.integer("thread_limit"),
    }
    _validate_job_limits(
        values["processing_seconds"], values["batch_size"], values["walltime_seconds"]
    )
    if values["cores"] != REQUIRED_CORES:
        raise GridOperatorError(f"job config must request exactly {REQUIRED_CORES} core")
    if values["thread_limit"] != 1:
        raise GridOperatorError("job config thread limit must be 1")
    return values


def _existing_bundle(paths: JobPaths) -> JobBundle | None:
    if paths.bundle.is_symlink():
        raise GridOperatorError(f"prepared bundle must not be a symlink: {paths.bundle}")
    if not paths.bundle.is_file():
        return None
    return read_bundle(paths.bundle)
