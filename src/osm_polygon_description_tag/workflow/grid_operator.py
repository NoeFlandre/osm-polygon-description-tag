"""Prepare, submit, reconcile, and collect one tiny Grid'5000 language job.

The operator writes its submission *intent* durably before it ever calls
``oarsub``. If the scheduler call then times out or answers without a job
identifier, the intent on disk is what proves a job may already exist, and the
only supported next step is reconciliation -- never a second submission.

Nothing here runs a scheduler command unless an explicit apply gate is passed;
without it every entry point returns a plan describing exactly what would be
done. All dependency installation and inference happen inside the generated job
script, which runs on allocated compute resources, never on a frontend.

This module is the public facade; the implementation lives in the focused
``grid_*`` modules next to it.
"""

from osm_polygon_description_tag.dataset.languages.checkpoint import (
    read_checkpoint as read_checkpoint,
)
from osm_polygon_description_tag.dataset.languages.checkpoint import shard_paths as shard_paths
from osm_polygon_description_tag.dataset.languages.snapshot import (
    SNAPSHOT_FILENAME as SNAPSHOT_FILENAME,
)
from osm_polygon_description_tag.runtime.serialization import (
    canonical_json_bytes as canonical_json_bytes,
)
from osm_polygon_description_tag.workflow.grid_collection import (
    _acknowledge_intent as _acknowledge_intent,
)
from osm_polygon_description_tag.workflow.grid_collection import (
    _copy_tree_entry as _copy_tree_entry,
)
from osm_polygon_description_tag.workflow.grid_collection import (
    _copy_tree_no_symlinks as _copy_tree_no_symlinks,
)
from osm_polygon_description_tag.workflow.grid_collection import (
    _prepare_collection_directories as _prepare_collection_directories,
)
from osm_polygon_description_tag.workflow.grid_collection import (
    _reject_overlapping_paths as _reject_overlapping_paths,
)
from osm_polygon_description_tag.workflow.grid_collection import (
    _require_checkpoint_extension as _require_checkpoint_extension,
)
from osm_polygon_description_tag.workflow.grid_collection import (
    _require_matching_committed_history as _require_matching_committed_history,
)
from osm_polygon_description_tag.workflow.grid_collection import (
    _require_matching_snapshots as _require_matching_snapshots,
)
from osm_polygon_description_tag.workflow.grid_collection import (
    _require_regular_directory as _require_regular_directory,
)
from osm_polygon_description_tag.workflow.grid_collection import (
    _validate_ack_identity as _validate_ack_identity,
)
from osm_polygon_description_tag.workflow.grid_collection import (
    _validate_ack_terminal as _validate_ack_terminal,
)
from osm_polygon_description_tag.workflow.grid_collection import (
    _validate_acknowledgment as _validate_acknowledgment,
)
from osm_polygon_description_tag.workflow.grid_collection import (
    acknowledge_collected_results,
    adopt_retrieved_intent,
    import_retrieved_results,
)
from osm_polygon_description_tag.workflow.grid_job_script import (
    _SHELL_METACHARACTERS as _SHELL_METACHARACTERS,
)
from osm_polygon_description_tag.workflow.grid_job_script import (
    _glotlid_model_path_for_snapshot as _glotlid_model_path_for_snapshot,
)
from osm_polygon_description_tag.workflow.grid_job_script import (
    _validate_batch_size as _validate_batch_size,
)
from osm_polygon_description_tag.workflow.grid_job_script import (
    _validate_processing_seconds as _validate_processing_seconds,
)
from osm_polygon_description_tag.workflow.grid_job_script import (
    _validate_remote_path as _validate_remote_path,
)
from osm_polygon_description_tag.workflow.grid_job_script import (
    _validate_walltime_seconds as _validate_walltime_seconds,
)
from osm_polygon_description_tag.workflow.grid_job_script import (
    render_job_script,
)
from osm_polygon_description_tag.workflow.grid_models import (
    BUNDLE_FILENAME,
    GRID_SUBMISSION_LOCK_FILENAME,
    INTENT_FILENAME,
    JOB_CONFIG_FILENAME,
    JOB_SCRIPT_FILENAME,
    STAGE_MANIFEST_FILENAME,
    GridOperatorError,
    JobBundle,
    JobNameResolver,
    JobPaths,
    PreparedJob,
    StagedFile,
    SubmissionIntent,
    bundle_for_shard,
    job_paths,
    read_bundle,
    read_intent,
    remote_child,
)
from osm_polygon_description_tag.workflow.grid_models import (
    INTENT_SCHEMA_VERSION as INTENT_SCHEMA_VERSION,
)
from osm_polygon_description_tag.workflow.grid_models import (
    QUARANTINE_DIRNAME as QUARANTINE_DIRNAME,
)
from osm_polygon_description_tag.workflow.grid_models import (
    STAGE_PROJECT_DIRNAME as STAGE_PROJECT_DIRNAME,
)
from osm_polygon_description_tag.workflow.grid_models import (
    STAGE_RUN_DIRNAME as STAGE_RUN_DIRNAME,
)
from osm_polygon_description_tag.workflow.grid_models import (
    STAGE_SOURCE_DIRNAME as STAGE_SOURCE_DIRNAME,
)
from osm_polygon_description_tag.workflow.grid_models import (
    _optional_text as _optional_text,
)
from osm_polygon_description_tag.workflow.grid_models import (
    _validate_relative_parquet as _validate_relative_parquet,
)
from osm_polygon_description_tag.workflow.grid_models import (
    _validate_stage_relative_file as _validate_stage_relative_file,
)
from osm_polygon_description_tag.workflow.grid_models import (
    jobs_root as jobs_root,
)
from osm_polygon_description_tag.workflow.grid_policy import (
    MAX_PROCESSING_SECONDS as MAX_PROCESSING_SECONDS,
)
from osm_polygon_description_tag.workflow.grid_policy import (
    MAX_WALLTIME_SECONDS as MAX_WALLTIME_SECONDS,
)
from osm_polygon_description_tag.workflow.grid_policy import REQUIRED_CORES as REQUIRED_CORES
from osm_polygon_description_tag.workflow.grid_prepare import (
    _job_config_payload as _job_config_payload,
)
from osm_polygon_description_tag.workflow.grid_prepare import (
    _read_job_config as _read_job_config,
)
from osm_polygon_description_tag.workflow.grid_prepare import (
    _verify_immutable_config as _verify_immutable_config,
)
from osm_polygon_description_tag.workflow.grid_prepare import (
    _verify_immutable_script as _verify_immutable_script,
)
from osm_polygon_description_tag.workflow.grid_prepare import (
    prepare_job,
)
from osm_polygon_description_tag.workflow.grid_scheduler import JobState as JobState
from osm_polygon_description_tag.workflow.grid_staging import (
    _copy_regular_file as _copy_regular_file,
)
from osm_polygon_description_tag.workflow.grid_staging import (
    _copy_resume_state as _copy_resume_state,
)
from osm_polygon_description_tag.workflow.grid_staging import (
    _copy_snapshot as _copy_snapshot,
)
from osm_polygon_description_tag.workflow.grid_staging import (
    _copy_source_shard as _copy_source_shard,
)
from osm_polygon_description_tag.workflow.grid_staging import (
    _existing_payload_for_resume as _existing_payload_for_resume,
)
from osm_polygon_description_tag.workflow.grid_staging import (
    _is_project_cache_file as _is_project_cache_file,
)
from osm_polygon_description_tag.workflow.grid_staging import (
    _payload_directories as _payload_directories,
)
from osm_polygon_description_tag.workflow.grid_staging import (
    _payload_file as _payload_file,
)
from osm_polygon_description_tag.workflow.grid_staging import (
    _payload_files as _payload_files,
)
from osm_polygon_description_tag.workflow.grid_staging import (
    _prepared_view as _prepared_view,
)
from osm_polygon_description_tag.workflow.grid_staging import (
    _project_files as _project_files,
)
from osm_polygon_description_tag.workflow.grid_staging import (
    _project_source_files as _project_source_files,
)
from osm_polygon_description_tag.workflow.grid_staging import (
    _project_source_root as _project_source_root,
)
from osm_polygon_description_tag.workflow.grid_staging import (
    _read_stage_manifest as _read_stage_manifest,
)
from osm_polygon_description_tag.workflow.grid_staging import (
    _require_payload_files as _require_payload_files,
)
from osm_polygon_description_tag.workflow.grid_staging import (
    _resume_files as _resume_files,
)
from osm_polygon_description_tag.workflow.grid_staging import (
    _resume_state_fingerprint as _resume_state_fingerprint,
)
from osm_polygon_description_tag.workflow.grid_staging import (
    _stage_descriptors as _stage_descriptors,
)
from osm_polygon_description_tag.workflow.grid_staging import (
    _staged_resume_fingerprint as _staged_resume_fingerprint,
)
from osm_polygon_description_tag.workflow.grid_staging import (
    _validate_staged_project as _validate_staged_project,
)
from osm_polygon_description_tag.workflow.grid_staging import (
    _validate_staged_snapshot as _validate_staged_snapshot,
)
from osm_polygon_description_tag.workflow.grid_staging import (
    _verify_one_staged_input as _verify_one_staged_input,
)
from osm_polygon_description_tag.workflow.grid_staging import (
    _verify_payload_identity as _verify_payload_identity,
)
from osm_polygon_description_tag.workflow.grid_staging import (
    _verify_payload_layout as _verify_payload_layout,
)
from osm_polygon_description_tag.workflow.grid_staging import (
    _verify_staged_file as _verify_staged_file,
)
from osm_polygon_description_tag.workflow.grid_staging import (
    prepare_portable_job,
    verify_prepared_bundle,
)
from osm_polygon_description_tag.workflow.grid_state import (
    _acquire_lock as _acquire_lock,
)
from osm_polygon_description_tag.workflow.grid_state import (
    _fsync_directory as _fsync_directory,
)
from osm_polygon_description_tag.workflow.grid_state import (
    _open_lock as _open_lock,
)
from osm_polygon_description_tag.workflow.grid_state import (
    _quarantine_artifact as _quarantine_artifact,
)
from osm_polygon_description_tag.workflow.grid_state import (
    _require_generated_artifact as _require_generated_artifact,
)
from osm_polygon_description_tag.workflow.grid_state import (
    _validate_resume_state as _validate_resume_state,
)
from osm_polygon_description_tag.workflow.grid_state import (
    collect_results,
    initialize_shard_checkpoint,
    quarantine_orphan_artifacts,
    submission_lock,
)
from osm_polygon_description_tag.workflow.grid_submission import (
    Reconciliation,
    SubmissionPlan,
    gather_policy_evidence,
    gather_policy_outputs,
    plan_submission,
    reconcile_job,
    resolve_job_name,
    submit_job,
)
from osm_polygon_description_tag.workflow.grid_submission import (
    _active_intent_reason as _active_intent_reason,
)
from osm_polygon_description_tag.workflow.grid_submission import (
    _captured as _captured,
)
from osm_polygon_description_tag.workflow.grid_submission import (
    _initial_checkpoint_block as _initial_checkpoint_block,
)
from osm_polygon_description_tag.workflow.grid_submission import (
    _intent_retry_block as _intent_retry_block,
)
from osm_polygon_description_tag.workflow.grid_submission import (
    _iter_intents as _iter_intents,
)
from osm_polygon_description_tag.workflow.grid_submission import (
    _policy_freshness_block as _policy_freshness_block,
)
from osm_polygon_description_tag.workflow.grid_submission import (
    _record_outcome as _record_outcome,
)
from osm_polygon_description_tag.workflow.grid_submission import (
    _record_terminal_reconciliation as _record_terminal_reconciliation,
)
from osm_polygon_description_tag.workflow.grid_submission import (
    _resolved_by_name as _resolved_by_name,
)
from osm_polygon_description_tag.workflow.grid_submission import (
    _run_wide_intent_block as _run_wide_intent_block,
)
from osm_polygon_description_tag.workflow.grid_submission import (
    _submission_request as _submission_request,
)
from osm_polygon_description_tag.workflow.grid_submission import (
    _validate_prepared_submission as _validate_prepared_submission,
)
from osm_polygon_description_tag.workflow.grid_submission import (
    _validate_submission_walltime as _validate_submission_walltime,
)
from osm_polygon_description_tag.workflow.grid_transport import (
    build_bundle_transfer_argv,
    build_result_retrieval_argv,
)

__all__ = [
    "BUNDLE_FILENAME",
    "GRID_SUBMISSION_LOCK_FILENAME",
    "INTENT_FILENAME",
    "JOB_CONFIG_FILENAME",
    "JOB_SCRIPT_FILENAME",
    "STAGE_MANIFEST_FILENAME",
    "GridOperatorError",
    "JobBundle",
    "JobNameResolver",
    "JobPaths",
    "PreparedJob",
    "Reconciliation",
    "StagedFile",
    "SubmissionIntent",
    "SubmissionPlan",
    "acknowledge_collected_results",
    "adopt_retrieved_intent",
    "build_bundle_transfer_argv",
    "build_result_retrieval_argv",
    "bundle_for_shard",
    "collect_results",
    "gather_policy_evidence",
    "gather_policy_outputs",
    "import_retrieved_results",
    "initialize_shard_checkpoint",
    "job_paths",
    "plan_submission",
    "prepare_job",
    "prepare_portable_job",
    "quarantine_orphan_artifacts",
    "read_bundle",
    "read_intent",
    "reconcile_job",
    "remote_child",
    "render_job_script",
    "resolve_job_name",
    "submission_lock",
    "submit_job",
    "verify_prepared_bundle",
]
