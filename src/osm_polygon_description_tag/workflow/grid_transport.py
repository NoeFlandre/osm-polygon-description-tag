"""Build the explicit rsync argv that moves bundles and results to and from a site."""

from pathlib import Path

from osm_polygon_description_tag.dataset.languages.checkpoint import (
    shard_paths,
)
from osm_polygon_description_tag.workflow.grid_job_script import (
    _validate_remote_path,
)
from osm_polygon_description_tag.workflow.grid_models import (
    GridOperatorError,
    PreparedJob,
    _validate_relative_parquet,
)
from osm_polygon_description_tag.workflow.grid_staging import (
    verify_prepared_bundle,
)


def build_bundle_transfer_argv(prepared: PreparedJob, remote_bundle_dir: str) -> tuple[str, ...]:
    """Return an explicit argv that transfers a verified bundle directory."""
    if not isinstance(prepared, PreparedJob):
        raise GridOperatorError("prepared bundle must be a PreparedJob")
    verify_prepared_bundle(prepared.payload_root, expected_bundle=prepared.bundle)
    _validate_remote_path(remote_bundle_dir, "remote bundle directory")
    destination = remote_bundle_dir.rstrip("/") or "/"
    return (
        "rsync",
        "--archive",
        "--checksum",
        "--protect-args",
        "--",
        f"{prepared.payload_root}/",
        f"{destination}/",
    )


def build_result_retrieval_argv(
    remote_run_dir: str, local_staging_dir: Path, shard: str
) -> tuple[str, ...]:
    """Return an explicit argv for retrieving one validated shard directory."""
    _validate_remote_path(remote_run_dir, "remote run directory")
    normalized_shard = _validate_relative_parquet(shard, "shard")
    if local_staging_dir.is_symlink() or (
        local_staging_dir.exists() and not local_staging_dir.is_dir()
    ):
        raise GridOperatorError(
            f"local staging directory is not a regular directory: {local_staging_dir}"
        )
    destination = shard_paths(local_staging_dir, normalized_shard).root
    remote_base = remote_run_dir.rstrip("/") or "/"
    remote_source = f"{remote_base}/shards/{destination.name}/"
    return (
        "rsync",
        "--archive",
        "--checksum",
        "--protect-args",
        "--",
        remote_source,
        f"{destination}/",
    )
