"""Prepare, submit, reconcile, and collect one tiny Grid'5000 language job.

The operator writes its submission *intent* durably before it ever calls
``oarsub``. If the scheduler call then times out or answers without a job
identifier, the intent on disk is what proves a job may already exist, and the
only supported next step is reconciliation -- never a second submission.

Nothing here runs a scheduler command unless an explicit apply gate is passed;
without it every entry point returns a plan describing exactly what would be
done. All dependency installation and inference happen inside the generated job
script, which runs on allocated compute resources, never on a frontend.
"""

import errno
import fcntl
import hashlib
import json
import os
import re
import shlex
import shutil
import tempfile
from collections.abc import Callable, Iterator, Mapping
from contextlib import contextmanager
from dataclasses import dataclass, replace
from datetime import UTC, datetime
from pathlib import Path
from typing import Final, TextIO, cast

from osm_polygon_description_tag.dataset.languages.atomic import (
    atomic_write_bytes,
    atomic_write_json,
    atomic_write_via,
    canonical_json_bytes,
)
from osm_polygon_description_tag.dataset.languages.checkpoint import (
    CheckpointError,
    ShardCheckpoint,
    ShardPaths,
    ShardStatus,
    exclusive_worker_lock,
    is_generated_part_name,
    is_generated_receipt_name,
    read_checkpoint,
    read_receipt,
    receipt_name_for_part,
    shard_paths,
    shards_root,
    write_checkpoint,
)
from osm_polygon_description_tag.dataset.languages.models import (
    CASCADE_DETECTOR_NAME,
    LINGUA_DETECTOR_NAME,
)
from osm_polygon_description_tag.dataset.languages.paths import relative_posix_path
from osm_polygon_description_tag.dataset.languages.payloads import PayloadReader, require_object
from osm_polygon_description_tag.dataset.languages.snapshot import (
    SNAPSHOT_FILENAME,
    SnapshotError,
    SnapshotManifest,
    fingerprint_lockfile,
    fingerprint_project_source,
    read_snapshot,
    source_path_for,
    verify_source_file,
)
from osm_polygon_description_tag.dataset.languages.validation import (
    RunReport,
    ShardReport,
    validate_run,
)
from osm_polygon_description_tag.dataset.manifest import file_sha256
from osm_polygon_description_tag.workflow.grid_policy import (
    MAX_PROCESSING_SECONDS,
    MAX_WALLTIME_SECONDS,
    REQUIRED_CORES,
    PolicyVerdict,
    policy_evidence_is_fresh,
)
from osm_polygon_description_tag.workflow.grid_scheduler import (
    DEFAULT_COMMAND_TIMEOUT,
    CommandRunner,
    JobState,
    SchedulerError,
    SubmissionOutcome,
    SubmissionRequest,
    SubmissionResult,
    build_oarsub_argv,
    job_state,
    resolve_account_job_name,
    run_command,
    submit,
)

JobNameResolver = Callable[[str], int | None]

INTENT_FILENAME: Final = "submission-intent.json"
BUNDLE_FILENAME: Final = "bundle.json"
JOB_SCRIPT_FILENAME: Final = "job.sh"
INTENT_SCHEMA_VERSION: Final = 1
BUNDLE_SCHEMA_VERSION: Final = 1
STAGE_SCHEMA_VERSION: Final = 1
JOB_CONFIG_SCHEMA_VERSION: Final = 1
GRID_SUBMISSION_LOCK_FILENAME: Final = ".grid-submission.lock"
STAGE_MANIFEST_FILENAME: Final = "stage.json"
JOB_CONFIG_FILENAME: Final = "job-config.json"
STAGE_PROJECT_DIRNAME: Final = "project"
STAGE_SOURCE_DIRNAME: Final = "source"
STAGE_RUN_DIRNAME: Final = "run"
QUARANTINE_DIRNAME: Final = "quarantine"
_FINGERPRINT_PATTERN = re.compile(r"[0-9a-f]{64}\Z")
_THREAD_LIMIT_VARIABLES: Final = (
    "OMP_NUM_THREADS",
    "OPENBLAS_NUM_THREADS",
    "MKL_NUM_THREADS",
    "NUMEXPR_NUM_THREADS",
    "RAYON_NUM_THREADS",
)


class GridOperatorError(ValueError):
    """Raised when job state on disk is missing, stale, or contradictory."""


@dataclass(frozen=True, slots=True)
class JobBundle:
    """Immutable identity of the code, lock, and one staged input shard."""

    snapshot_id: str
    model_config_fingerprint: str
    code_fingerprint: str
    lock_fingerprint: str
    shard: str
    source_sha256: str
    source_size_bytes: int
    input_row_count: int

    def __post_init__(self) -> None:
        _validate_fingerprint(self.snapshot_id, "snapshot id")
        _validate_fingerprint(self.model_config_fingerprint, "model configuration fingerprint")
        _validate_fingerprint(self.code_fingerprint, "code fingerprint")
        _validate_fingerprint(self.lock_fingerprint, "lock fingerprint")
        _validate_relative_parquet(self.shard, "shard")
        _validate_fingerprint(self.source_sha256, "source sha256")
        _validate_non_negative_int(self.source_size_bytes, "source size")
        _validate_non_negative_int(self.input_row_count, "input row count")

    @property
    def bundle_id(self) -> str:
        """Return the derived identity of everything this job depends on."""
        return hashlib.sha256(canonical_json_bytes(self._identity_payload())).hexdigest()

    def _identity_payload(self) -> dict[str, object]:
        return {
            "bundle_schema_version": BUNDLE_SCHEMA_VERSION,
            "snapshot_id": self.snapshot_id,
            "model_config_fingerprint": self.model_config_fingerprint,
            "code_fingerprint": self.code_fingerprint,
            "lock_fingerprint": self.lock_fingerprint,
            "shard": self.shard,
            "source_sha256": self.source_sha256,
            "source_size_bytes": self.source_size_bytes,
            "input_row_count": self.input_row_count,
        }

    def to_payload(self) -> dict[str, object]:
        return {"bundle_id": self.bundle_id, **self._identity_payload()}

    @classmethod
    def from_payload(cls, payload: object) -> "JobBundle":
        """Rebuild a bundle, rejecting a payload whose identity does not hold."""
        reader = require_object(payload, error=GridOperatorError, label="bundle")
        if reader.integer("bundle_schema_version") != BUNDLE_SCHEMA_VERSION:
            raise GridOperatorError("unsupported bundle schema version")
        bundle = cls(
            snapshot_id=reader.text("snapshot_id"),
            model_config_fingerprint=reader.text("model_config_fingerprint"),
            code_fingerprint=reader.text("code_fingerprint"),
            lock_fingerprint=reader.text("lock_fingerprint"),
            shard=reader.text("shard"),
            source_sha256=reader.text("source_sha256"),
            source_size_bytes=reader.integer("source_size_bytes"),
            input_row_count=reader.integer("input_row_count"),
        )
        if reader.text("bundle_id") != bundle.bundle_id:
            raise GridOperatorError("bundle id does not match its canonical content")
        return bundle


def bundle_for_shard(snapshot: SnapshotManifest, shard: str) -> JobBundle:
    """Bind one snapshot-listed shard to the code and lock that will process it."""
    source = snapshot.source_file(shard)
    return JobBundle(
        snapshot_id=snapshot.snapshot_id,
        model_config_fingerprint=snapshot.model_config_fingerprint,
        code_fingerprint=snapshot.code_fingerprint,
        lock_fingerprint=snapshot.lock_fingerprint,
        shard=source.relative_path,
        source_sha256=source.sha256,
        source_size_bytes=source.size_bytes,
        input_row_count=source.row_count,
    )


def _validate_fingerprint(value: object, label: str) -> None:
    if not isinstance(value, str) or _FINGERPRINT_PATTERN.fullmatch(value) is None:
        raise GridOperatorError(f"{label} must be a lowercase SHA-256 hex fingerprint")


def _validate_non_negative_int(value: object, label: str) -> None:
    if type(value) is not int or value < 0:
        raise GridOperatorError(f"{label} must be a non-negative integer")


def _validate_relative_parquet(value: object, label: str) -> str:
    normalized = relative_posix_path(value, error=GridOperatorError, label=label)
    if Path(normalized).suffix != ".parquet":
        raise GridOperatorError(f"{label} must be a Parquet path")
    return normalized


def _validate_stage_relative_file(value: object) -> str:
    return relative_posix_path(value, error=GridOperatorError, label="staged file path")


@dataclass(frozen=True, slots=True)
class SubmissionIntent:
    """The durable record that a submission was about to be attempted."""

    bundle_id: str
    shard: str
    job_name: str
    walltime_seconds: int
    cores: int
    recorded_at: str
    job_id: int | None = None
    outcome: str | None = None
    detail: str | None = None
    attempt: int = 1
    terminal_state: str | None = None
    reconciled_at: str | None = None
    result_acknowledged: bool = False
    result_complete: bool = False
    collected_at: str | None = None

    @property
    def is_unresolved(self) -> bool:
        """Return whether a job may exist whose identifier is not yet known."""
        return (
            self.terminal_state is None
            and self.job_id is None
            and self.outcome != str(SubmissionOutcome.REJECTED)
        )

    @property
    def is_active_or_ambiguous(self) -> bool:
        """Return whether this attempt still reserves the run for one job."""
        return self.terminal_state is None and self.outcome != str(SubmissionOutcome.REJECTED)

    @property
    def can_retry(self) -> bool:
        """Return whether acknowledged incomplete results may be attempted again."""
        return (
            self.terminal_state == str(JobState.TERMINATED)
            and self.result_acknowledged
            and not self.result_complete
        )

    def to_payload(self) -> dict[str, object]:
        return {
            "intent_schema_version": INTENT_SCHEMA_VERSION,
            "bundle_id": self.bundle_id,
            "shard": self.shard,
            "job_name": self.job_name,
            "walltime_seconds": self.walltime_seconds,
            "cores": self.cores,
            "recorded_at": self.recorded_at,
            "job_id": self.job_id,
            "outcome": self.outcome,
            "detail": self.detail,
            "attempt": self.attempt,
            "terminal_state": self.terminal_state,
            "reconciled_at": self.reconciled_at,
            "result_acknowledged": self.result_acknowledged,
            "result_complete": self.result_complete,
            "collected_at": self.collected_at,
        }

    @classmethod
    def from_payload(cls, payload: object) -> "SubmissionIntent":
        """Rebuild an intent, rejecting any malformed field."""
        reader = require_object(payload, error=GridOperatorError, label="intent")
        _validate_intent_schema(reader)
        raw_job_id = _intent_job_id(reader)
        raw = cast(Mapping[str, object], payload)
        return _intent_from_reader(cls, reader, raw, raw_job_id)


def _validate_intent_schema(reader: PayloadReader) -> None:
    if reader.integer("intent_schema_version") != INTENT_SCHEMA_VERSION:
        raise GridOperatorError("unsupported intent schema version")


def _intent_job_id(reader: PayloadReader) -> int | None:
    raw_job_id = reader.raw("job_id")
    if raw_job_id is None:
        return None
    if type(raw_job_id) is not int:
        raise GridOperatorError("intent field job_id must be an integer or null")
    if raw_job_id < 1:
        raise GridOperatorError("intent field job_id must be a positive integer")
    return raw_job_id


def _intent_from_reader(
    intent_type: type[SubmissionIntent],
    reader: PayloadReader,
    raw: Mapping[str, object],
    job_id: int | None,
) -> SubmissionIntent:
    intent = intent_type(
        bundle_id=reader.text("bundle_id"),
        shard=reader.text("shard"),
        job_name=reader.text("job_name"),
        walltime_seconds=reader.integer("walltime_seconds"),
        cores=reader.integer("cores"),
        recorded_at=reader.text("recorded_at"),
        job_id=job_id,
        outcome=_optional_text(reader.raw("outcome"), "outcome"),
        detail=_optional_text(reader.raw("detail"), "detail"),
        attempt=_optional_integer(raw.get("attempt", 1), "attempt", minimum=1),
        terminal_state=_optional_text(raw.get("terminal_state"), "terminal_state"),
        reconciled_at=_optional_text(raw.get("reconciled_at"), "reconciled_at"),
        result_acknowledged=_strict_bool(
            raw.get("result_acknowledged", False), "result_acknowledged"
        ),
        result_complete=_strict_bool(raw.get("result_complete", False), "result_complete"),
        collected_at=_optional_text(raw.get("collected_at"), "collected_at"),
    )
    _validate_intent_consistency(intent)
    return intent


def _optional_text(value: object, label: str) -> str | None:
    if value is None or isinstance(value, str):
        return value
    raise GridOperatorError(f"intent field {label} must be a string or null")


def _optional_integer(value: object, label: str, *, minimum: int) -> int:
    if type(value) is not int or value < minimum:
        raise GridOperatorError(f"intent field {label} must be an integer >= {minimum}")
    return value


def _strict_bool(value: object, label: str) -> bool:
    if type(value) is not bool:
        raise GridOperatorError(f"intent field {label} must be a boolean")
    return value


def _validate_intent_consistency(intent: SubmissionIntent) -> None:
    _validate_intent_outcome(intent)
    _validate_intent_terminal(intent)
    _validate_intent_collection(intent)
    _validate_rejected_intent(intent)


def _validate_intent_outcome(intent: SubmissionIntent) -> None:
    outcomes = {None, *(str(item) for item in SubmissionOutcome)}
    if intent.outcome not in outcomes:
        raise GridOperatorError(f"unsupported submission outcome: {intent.outcome!r}")


def _validate_intent_terminal(intent: SubmissionIntent) -> None:
    if intent.terminal_state is not None and intent.terminal_state != str(JobState.TERMINATED):
        raise GridOperatorError("intent terminal_state must be terminated")
    if intent.terminal_state is not None and intent.job_id is None:
        raise GridOperatorError("terminal intent must record a job identifier")


def _validate_intent_collection(intent: SubmissionIntent) -> None:
    if intent.result_acknowledged and intent.terminal_state is None:
        raise GridOperatorError("collected results require a terminal scheduler state")
    if intent.result_complete and not intent.result_acknowledged:
        raise GridOperatorError("complete results require a collection acknowledgment")


def _validate_rejected_intent(intent: SubmissionIntent) -> None:
    if intent.outcome == str(SubmissionOutcome.REJECTED) and intent.job_id is not None:
        raise GridOperatorError("rejected intent must not record a job identifier")


@dataclass(frozen=True, slots=True)
class JobPaths:
    """Owned filesystem locations for one job attempt."""

    root: Path
    bundle: Path
    intent: Path
    script: Path

    @property
    def run_dir(self) -> Path:
        """Return the run root containing this job directory."""
        return self.root.parent.parent

    @property
    def config(self) -> Path:
        """Return the immutable execution-limit record for this job."""
        return self.root / JOB_CONFIG_FILENAME

    @property
    def payload_root(self) -> Path:
        """Return the local portable payload directory for this job."""
        return self.root / "payload"


@dataclass(frozen=True, slots=True)
class PreparedJob:
    """A local payload whose bytes are ready for an injected transport."""

    bundle: JobBundle
    paths: JobPaths
    payload_root: Path
    project_root: Path
    source_root: Path
    run_root: Path
    manifest: Path


@dataclass(frozen=True, slots=True)
class StagedFile:
    """One content-addressed file recorded in a portable payload manifest."""

    relative_path: str
    size_bytes: int
    sha256: str

    def __post_init__(self) -> None:
        _validate_stage_relative_file(self.relative_path)
        _validate_non_negative_int(self.size_bytes, "staged file size")
        _validate_fingerprint(self.sha256, "staged file sha256")

    def to_payload(self) -> dict[str, object]:
        return {
            "relative_path": self.relative_path,
            "size_bytes": self.size_bytes,
            "sha256": self.sha256,
        }

    @classmethod
    def from_payload(cls, payload: object) -> "StagedFile":
        reader = require_object(payload, error=GridOperatorError, label="staged file")
        return cls(
            relative_path=reader.text("relative_path"),
            size_bytes=reader.integer("size_bytes"),
            sha256=reader.text("sha256"),
        )


def jobs_root(run_dir: Path) -> Path:
    """Return the single directory that owns every prepared job."""
    return run_dir / "jobs"


def job_paths(run_dir: Path, bundle: JobBundle) -> JobPaths:
    """Return deterministic owned job paths without creating anything."""
    root = jobs_root(run_dir) / bundle.bundle_id[:32]
    return JobPaths(
        root,
        root / BUNDLE_FILENAME,
        root / INTENT_FILENAME,
        root / JOB_SCRIPT_FILENAME,
    )


def _validated_script_inputs(
    remote_project_dir: str,
    remote_source_dir: str,
    remote_run_dir: str,
    processing_seconds: int,
    batch_size: int,
    walltime_seconds: int,
    remote_bundle_dir: str | None,
    sat_model_path: str,
    glotlid_model_path: str | None,
) -> str:
    for path, label in (
        (remote_project_dir, "remote project directory"),
        (remote_source_dir, "remote source directory"),
        (remote_run_dir, "remote run directory"),
    ):
        _validate_remote_path(path, label)
    bundle_dir = remote_bundle_dir or str(Path(remote_project_dir).parent)
    _validate_remote_path(bundle_dir, "remote bundle directory")
    _validate_job_limits(processing_seconds, batch_size, walltime_seconds)
    _validate_remote_path(sat_model_path, "remote SaT model path")
    if glotlid_model_path is not None:
        _validate_remote_path(glotlid_model_path, "remote GlotLID model path")
    return bundle_dir


def render_job_script(
    bundle: JobBundle,
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
) -> str:
    """Render the script the job runs on its allocated compute node.

    Dependency installation and inference both happen here, inside the job, so
    a frontend is never used for anything but file management and scheduling.
    """
    bundle_dir = _validated_script_inputs(
        remote_project_dir,
        remote_source_dir,
        remote_run_dir,
        processing_seconds,
        batch_size,
        walltime_seconds,
        remote_bundle_dir,
        sat_model_path,
        glotlid_model_path,
    )
    exports = "\n".join(f"export {name}=1" for name in _THREAD_LIMIT_VARIABLES)
    quoted_project = shlex.quote(remote_project_dir)
    quoted_source = shlex.quote(remote_source_dir)
    quoted_run = shlex.quote(remote_run_dir)
    quoted_shard = shlex.quote(bundle.shard)
    line_continuation = "\\" + "\n"
    sat_option = f" {line_continuation}  --sat-model-path {shlex.quote(sat_model_path)}"
    glotlid_option = ""
    if glotlid_model_path is not None:
        glotlid_option = (
            f" {line_continuation}  --glotlid-model-path {shlex.quote(glotlid_model_path)}"
        )
    scratch_prefix = f"osm-polygon-description-tag-{bundle.bundle_id[:16]}.XXXXXX"
    verify_step = _render_payload_verification(bundle_dir) if remote_bundle_dir is not None else ""
    return f"""#!/usr/bin/env bash
# Generated for bundle {bundle.bundle_id}
# Shard: {bundle.shard}
set -euo pipefail

{exports}

job_tmp_root="$(mktemp -d "${{TMPDIR:-/tmp}}/{scratch_prefix}")"
trap 'rm -rf -- "$job_tmp_root"' EXIT HUP INT TERM
export UV_PROJECT_ENVIRONMENT="$job_tmp_root/venv"
export UV_CACHE_DIR="$job_tmp_root/uv-cache"
export XDG_CACHE_HOME="$job_tmp_root/xdg-cache"
export XDG_DATA_HOME="$job_tmp_root/xdg-data"
export UV_PYTHON_INSTALL_DIR="$job_tmp_root/python"
export HF_HOME="$job_tmp_root/hf"
export HF_DATASETS_CACHE="$job_tmp_root/hf/datasets"
export HUGGINGFACE_HUB_CACHE="$job_tmp_root/hf/hub"
export TRANSFORMERS_CACHE="$job_tmp_root/hf/transformers"
export MPLCONFIGDIR="$job_tmp_root/matplotlib"
export PYTHONPYCACHEPREFIX="$job_tmp_root/pycache"
export PYTHONDONTWRITEBYTECODE=1

for variable in {" ".join(_THREAD_LIMIT_VARIABLES)}; do
  if [[ "${{!variable}}" != "1" ]]; then
    echo "thread limit $variable is not 1" >&2
    exit 64
  fi
done

cd {quoted_project}
export PATH="${{PATH}}:$HOME/.local/bin"
uv sync --frozen --no-dev --extra language
{verify_step}

uv run --no-sync osm-polygon-description-tag language run \\
  --source-root {quoted_source} \\
  --run-dir {quoted_run} \\
  --shard {quoted_shard} \\
  --batch-size {batch_size} \\
  --budget-seconds {processing_seconds}{sat_option}{glotlid_option}

uv run --no-sync osm-polygon-description-tag language validate \\
  --run-dir {quoted_run} \\
  --shard {quoted_shard}
"""


def _render_payload_verification(bundle_dir: str) -> str:
    verify_code = (
        "import sys; from pathlib import Path; "
        "from osm_polygon_description_tag.workflow.grid_operator import "
        "verify_prepared_bundle; verify_prepared_bundle(Path(sys.argv[1]))"
    )
    return f"uv run --no-sync python -c {shlex.quote(verify_code)} {shlex.quote(bundle_dir)}"


_SHELL_METACHARACTERS: Final = (
    "\x00",
    "\n",
    "\r",
    " ",
    "'",
    '"',
    "$",
    "`",
    ";",
    "&",
    "|",
    ">",
    "<",
    "(",
    ")",
    "{",
    "}",
    "[",
    "]",
    "*",
    "?",
    "!",
    "\\",
)


def _validate_remote_path(value: str, label: str) -> None:
    _require_absolute(value, label)
    if any(character in value for character in _SHELL_METACHARACTERS):
        raise GridOperatorError(f"{label} must not contain shell metacharacters")
    # ``PurePath`` drops every ``.`` component while parsing, so ``..`` is the only
    # traversal component a parsed absolute path can still carry.
    if ".." in Path(value).parts:
        raise GridOperatorError(f"{label} must not contain traversal components")


def _validate_job_limits(
    processing_seconds: object, batch_size: object, walltime_seconds: object
) -> None:
    processing = _validate_processing_seconds(processing_seconds)
    _validate_batch_size(batch_size)
    walltime = _validate_walltime_seconds(walltime_seconds)
    if processing >= walltime:
        raise GridOperatorError("processing budget must be less than walltime")


def _validate_processing_seconds(value: object) -> int:
    if type(value) is not int or not 0 < value <= MAX_PROCESSING_SECONDS:
        raise GridOperatorError(
            f"processing budget must be between 1 and {MAX_PROCESSING_SECONDS} seconds"
        )
    return value


def _validate_batch_size(value: object) -> int:
    if type(value) is not int or value < 1:
        raise GridOperatorError("batch size must be a positive integer")
    return value


def _validate_walltime_seconds(value: object) -> int:
    if type(value) is not int or not 0 < value <= MAX_WALLTIME_SECONDS:
        raise GridOperatorError(f"walltime must be between 1 and {MAX_WALLTIME_SECONDS} seconds")
    return value


def _require_absolute(value: str, label: str) -> None:
    if not isinstance(value, str) or not value:
        raise GridOperatorError(f"{label} must be a non-empty string")
    if not value.startswith("/"):
        raise GridOperatorError(f"{label} must be an absolute path")


def _glotlid_model_path_for_snapshot(
    snapshot: SnapshotManifest,
    glotlid_model_path: str | None,
) -> str | None:
    detector_name = snapshot.model_identity.detector_name
    if detector_name == CASCADE_DETECTOR_NAME:
        if glotlid_model_path is None:
            raise GridOperatorError("cascade job requires a GlotLID model path")
        return glotlid_model_path
    if detector_name == LINGUA_DETECTOR_NAME:
        if glotlid_model_path is not None:
            raise GridOperatorError("GlotLID model path requires a cascade snapshot")
        return None
    raise GridOperatorError(f"unsupported detector for Grid jobs: {detector_name!r}")


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


@contextmanager
def _state_locks(run_dir: Path, *, submission_locked: bool) -> Iterator[None]:
    """Hold the submission lock, then the worker lock, in that order."""
    if submission_locked:
        with exclusive_worker_lock(run_dir):
            yield
        return
    with _both_state_locks(run_dir):
        yield


@contextmanager
def _both_state_locks(run_dir: Path) -> Iterator[None]:
    """Hold the run-wide submission lock, then this run's worker lock."""
    with submission_lock(run_dir), exclusive_worker_lock(run_dir):
        yield


def initialize_shard_checkpoint(
    run_dir: Path,
    bundle: JobBundle,
    *,
    batch_size: int,
    submission_locked: bool = False,
) -> ShardCheckpoint:
    """Create, or confirm, the zero-cursor checkpoint of one prepared shard.

    A job that dies during setup then still leaves a validated resumable
    position behind, so its results can be collected and acknowledged. An
    existing checkpoint is never rewritten; it is only checked against the
    bundle it must belong to.
    """
    _validate_batch_size(batch_size)
    with _state_locks(run_dir, submission_locked=submission_locked):
        return _initialized_checkpoint(run_dir, bundle, batch_size)


def _initialized_checkpoint(run_dir: Path, bundle: JobBundle, batch_size: int) -> ShardCheckpoint:
    state = shard_paths(run_dir, bundle.shard)
    _validate_resume_root(state)
    existing = _shard_checkpoint(state)
    if existing is not None:
        _require_bound_checkpoint(existing, bundle)
        return existing
    if _resume_root_has_children(state):
        raise GridOperatorError("resume artifacts exist without a checkpoint")
    checkpoint = _initial_checkpoint(bundle, batch_size)
    write_checkpoint(state.checkpoint, checkpoint)
    return checkpoint


def _initial_checkpoint(bundle: JobBundle, batch_size: int) -> ShardCheckpoint:
    consumed = bundle.input_row_count == 0
    return ShardCheckpoint(
        snapshot_id=bundle.snapshot_id,
        model_config_fingerprint=bundle.model_config_fingerprint,
        shard=bundle.shard,
        batch_size=batch_size,
        input_row_count=bundle.input_row_count,
        input_cursor=0,
        annotation_count=0,
        completed_parts=(),
        status=ShardStatus.COMPLETE if consumed else ShardStatus.PAUSED,
    )


def _shard_checkpoint(state: ShardPaths) -> ShardCheckpoint | None:
    """Read the shard checkpoint, failing closed on anything unusable."""
    if state.checkpoint.is_symlink():
        raise GridOperatorError("resume checkpoint must not be a symlink")
    if not state.checkpoint.exists():
        return None
    if not state.checkpoint.is_file():
        raise GridOperatorError("resume checkpoint must be a regular file")
    try:
        return read_checkpoint(state.checkpoint)
    except CheckpointError as error:
        raise GridOperatorError(f"shard checkpoint is unusable: {error}") from error


def _checkpoint_identity(checkpoint: ShardCheckpoint) -> tuple[str, str, str, int]:
    return (
        checkpoint.snapshot_id,
        checkpoint.model_config_fingerprint,
        checkpoint.shard,
        checkpoint.input_row_count,
    )


def _bundle_identity(bundle: JobBundle) -> tuple[str, str, str, int]:
    return (
        bundle.snapshot_id,
        bundle.model_config_fingerprint,
        bundle.shard,
        bundle.input_row_count,
    )


def _require_bound_checkpoint(checkpoint: ShardCheckpoint, bundle: JobBundle) -> None:
    if _checkpoint_identity(checkpoint) != _bundle_identity(bundle):
        raise GridOperatorError("existing shard checkpoint is not bound to this job bundle")


def quarantine_orphan_artifacts(run_dir: Path, shard: str) -> tuple[str, ...]:
    """Move generated artifacts the checkpoint does not list out of the way.

    A worker that died between writing a part or receipt and committing its
    checkpoint leaves artifacts the authoritative checkpoint knows nothing
    about. They are never adopted and never deleted: they are preserved under
    ``quarantine/`` so the checkpoint-listed state can be restaged, collected,
    and resumed. Anything that is not a recognized generated artifact keeps
    failing closed.
    """
    with _both_state_locks(run_dir):
        return _quarantined_orphans(run_dir, shard)


def _quarantined_orphans(run_dir: Path, shard: str) -> tuple[str, ...]:
    state = shard_paths(run_dir, shard)
    _validate_resume_root(state)
    if not _resume_checkpoint_is_present(state):
        return ()
    checkpoint = read_checkpoint(state.checkpoint)
    _validate_resume_layout(state)
    orphans = _orphan_artifacts(state, checkpoint)
    for relative in orphans:
        _quarantine_artifact(state, relative)
    return orphans


def _orphan_artifacts(state: ShardPaths, checkpoint: ShardCheckpoint) -> tuple[str, ...]:
    groups = (
        (state.parts, set(checkpoint.completed_parts), is_generated_part_name),
        (
            state.receipts,
            {receipt_name_for_part(name) for name in checkpoint.completed_parts},
            is_generated_receipt_name,
        ),
    )
    return tuple(
        f"{directory.name}/{path.name}"
        for directory, committed, recognized in groups
        for path in _generated_artifacts(directory, recognized)
        if path.name not in committed
    )


def _generated_artifacts(directory: Path, recognized: Callable[[str], bool]) -> tuple[Path, ...]:
    """List one artifact directory, refusing anything a worker never writes."""
    if not directory.exists():
        return ()
    children = tuple(sorted(directory.iterdir()))
    for child in children:
        _require_generated_artifact(child, recognized)
    return children


def _require_generated_artifact(path: Path, recognized: Callable[[str], bool]) -> None:
    if path.is_symlink() or not path.is_file():
        raise GridOperatorError(f"shard state contains an unusable artifact: {path.name}")
    if not recognized(path.name):
        raise GridOperatorError(f"shard state contains an unknown artifact: {path.name}")


def _quarantine_artifact(state: ShardPaths, relative: str) -> None:
    destination = state.root / QUARANTINE_DIRNAME / relative
    if destination.exists() or destination.is_symlink():
        raise GridOperatorError(f"quarantine already holds an artifact: {relative}")
    destination.parent.mkdir(parents=True, exist_ok=True)
    os.replace(state.root / relative, destination)
    _fsync_directory(destination.parent)


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


def read_bundle(path: Path) -> JobBundle:
    """Read and validate one prepared bundle."""
    return JobBundle.from_payload(_read_json(path, "bundle"))


def read_intent(path: Path) -> SubmissionIntent:
    """Read and validate one durable submission intent."""
    return SubmissionIntent.from_payload(_read_json(path, "intent"))


def _read_json(path: Path, label: str) -> object:
    try:
        return json.loads(path.read_text(encoding="utf-8"))  # pragma: no mutate - codec alias only
    except (OSError, UnicodeError, json.JSONDecodeError) as error:
        raise GridOperatorError(f"cannot read {label} {path}: {error}") from error


def prepare_portable_job(
    run_dir: Path,
    project_root: Path,
    source_dir: Path,
    snapshot: SnapshotManifest,
    shard: str,
    *,
    remote_bundle_dir: str,
    sat_model_path: str,
    processing_seconds: int = MAX_PROCESSING_SECONDS,
    batch_size: int = 512,
    walltime_seconds: int = MAX_WALLTIME_SECONDS,
    glotlid_model_path: str | None = None,
) -> PreparedJob:
    """Prepare a self-contained local payload for an injected transport.

    The payload contains the generated job, project code and lock, exactly one
    snapshot-listed source shard, and every validated resume artifact. No
    transport is called here.
    """
    _validate_remote_path(remote_bundle_dir, "remote bundle directory")
    base = remote_bundle_dir.rstrip("/") or "/"
    remote_project = _remote_child(base, STAGE_PROJECT_DIRNAME)
    remote_source = _remote_child(base, STAGE_SOURCE_DIRNAME)
    remote_run = _remote_child(base, STAGE_RUN_DIRNAME)
    with submission_lock(run_dir):
        bundle, paths = prepare_job(
            run_dir,
            snapshot,
            shard,
            remote_project_dir=remote_project,
            remote_source_dir=remote_source,
            remote_run_dir=remote_run,
            processing_seconds=processing_seconds,
            batch_size=batch_size,
            walltime_seconds=walltime_seconds,
            remote_bundle_dir=base,
            sat_model_path=sat_model_path,
            glotlid_model_path=glotlid_model_path,
            submission_locked=True,
        )
        return _stage_portable_payload(paths, bundle, project_root, source_dir, snapshot)


def _remote_child(base: str, name: str) -> str:
    return f"/{name}" if base == "/" else f"{base}/{name}"


def _stage_portable_payload(
    paths: JobPaths,
    bundle: JobBundle,
    project_root: Path,
    source_dir: Path,
    snapshot: SnapshotManifest,
) -> PreparedJob:
    _require_payload_root(paths.payload_root)
    _verify_stage_inputs(paths, bundle, project_root, source_dir, snapshot)
    resume_fingerprint = _resume_state_fingerprint(paths.run_dir, bundle.shard)
    payload_root = _select_payload_root(paths, bundle, resume_fingerprint)
    if payload_root.exists():
        return _prepared_view(paths, bundle, payload_root)
    _materialize_payload(
        payload_root,
        paths,
        bundle,
        project_root,
        source_dir,
        snapshot,
        resume_fingerprint,
    )
    return _prepared_view(paths, bundle, payload_root)


def _require_payload_root(payload_root: Path) -> None:
    if payload_root.is_symlink():
        raise GridOperatorError(f"portable payload must not be a symlink: {payload_root}")


def _select_payload_root(paths: JobPaths, bundle: JobBundle, resume_fingerprint: str) -> Path:
    existing = _existing_payload_for_resume(paths.payload_root, bundle, resume_fingerprint)
    if existing is not None:
        return existing
    versioned = paths.root / f"payload-{resume_fingerprint[:16]}"
    _require_payload_root(versioned)
    existing = _existing_payload_for_resume(versioned, bundle, resume_fingerprint)
    return versioned if existing is None else existing


def _existing_payload_for_resume(
    payload_root: Path, bundle: JobBundle, resume_fingerprint: str
) -> Path | None:
    if not payload_root.exists():
        return None
    if _staged_resume_fingerprint(payload_root) != resume_fingerprint:
        return None
    verify_prepared_bundle(payload_root, expected_bundle=bundle)
    return payload_root


def _materialize_payload(
    payload_root: Path,
    paths: JobPaths,
    bundle: JobBundle,
    project_root: Path,
    source_dir: Path,
    snapshot: SnapshotManifest,
    resume_fingerprint: str,
) -> None:
    temporary = Path(tempfile.mkdtemp(prefix=".payload-", dir=paths.root))
    try:
        _copy_payload_inputs(temporary, paths, bundle, project_root, source_dir, snapshot)
        _write_stage_manifest(temporary, bundle, resume_fingerprint)
        os.replace(temporary, payload_root)
        _fsync_directory(paths.root)
    except (OSError, SnapshotError, CheckpointError, GridOperatorError):
        shutil.rmtree(temporary, ignore_errors=True)
        raise


def _copy_payload_inputs(
    temporary: Path,
    paths: JobPaths,
    bundle: JobBundle,
    project_root: Path,
    source_dir: Path,
    snapshot: SnapshotManifest,
) -> None:
    for source in (paths.bundle, paths.config, paths.script):
        _copy_regular_file(source, temporary / source.name)
    _copy_project(project_root, temporary / STAGE_PROJECT_DIRNAME)
    _copy_source_shard(snapshot, source_dir, temporary / STAGE_SOURCE_DIRNAME, bundle.shard)
    _copy_snapshot(paths.run_dir, temporary / STAGE_RUN_DIRNAME)
    _copy_resume_state(paths.run_dir, temporary / STAGE_RUN_DIRNAME, bundle.shard)


def _prepared_view(paths: JobPaths, bundle: JobBundle, root: Path) -> PreparedJob:
    return PreparedJob(
        bundle=bundle,
        paths=paths,
        payload_root=root,
        project_root=root / STAGE_PROJECT_DIRNAME,
        source_root=root / STAGE_SOURCE_DIRNAME,
        run_root=root / STAGE_RUN_DIRNAME,
        manifest=root / STAGE_MANIFEST_FILENAME,
    )


def _verify_stage_inputs(
    paths: JobPaths,
    bundle: JobBundle,
    project_root: Path,
    source_dir: Path,
    snapshot: SnapshotManifest,
) -> None:
    try:
        if read_snapshot(paths.run_dir) != snapshot:
            raise GridOperatorError("run snapshot does not match the requested portable bundle")
        if fingerprint_project_source(project_root) != bundle.code_fingerprint:
            raise GridOperatorError("project source does not match the bundle code fingerprint")
        if fingerprint_lockfile(project_root) != bundle.lock_fingerprint:
            raise GridOperatorError("project lockfile does not match the bundle lock fingerprint")
        verify_source_file(snapshot, source_dir, bundle.shard)
    except SnapshotError as error:
        raise GridOperatorError(str(error)) from error


def _copy_regular_file(source: Path, destination: Path) -> None:
    if source.is_symlink() or not source.is_file():
        raise GridOperatorError(f"staging input must be a regular file: {source}")
    destination.parent.mkdir(parents=True, exist_ok=True)
    try:
        shutil.copyfile(source, destination)
        shutil.copymode(source, destination)
    except OSError as error:
        raise GridOperatorError(f"cannot stage {source}: {error}") from error


def _project_source_root(project_root: Path) -> Path:
    """Return the one source directory a portable payload carries."""
    return project_root / "src"


def _project_files(project_root: Path) -> tuple[Path, ...]:
    required = (project_root / "pyproject.toml", project_root / "uv.lock")
    _require_project_files(required)
    candidates = [*required]
    readme = project_root / "README.md"
    optional_readme = _optional_project_readme(readme)
    if optional_readme is not None:
        candidates.append(optional_readme)
    source = _project_source_root(project_root)
    _require_project_source(source)
    candidates.extend(_project_source_files(source))
    return tuple(candidates)


def _require_project_files(required: tuple[Path, ...]) -> None:
    for path in required:
        if path.is_symlink() or not path.is_file():
            raise GridOperatorError(f"project staging input is missing: {path}")


def _optional_project_readme(readme: Path) -> Path | None:
    if not readme.exists():
        return None
    if readme.is_symlink() or not readme.is_file():
        raise GridOperatorError(f"project staging input is not a regular file: {readme}")
    return readme


def _require_project_source(source: Path) -> None:
    if source.is_symlink() or not source.is_dir():
        raise GridOperatorError(f"project source directory is missing: {source}")


def _project_source_files(source: Path) -> tuple[Path, ...]:
    candidates: list[Path] = []
    for path in sorted(source.rglob("*"), key=lambda item: item.relative_to(source).as_posix()):
        candidate = _project_source_file(path, source)
        if candidate is not None:
            candidates.append(candidate)
    return tuple(candidates)


def _project_source_file(path: Path, source: Path) -> Path | None:
    relative = path.relative_to(source)
    if _is_project_cache_file(path, relative):
        return None
    if path.is_symlink():
        raise GridOperatorError(f"project source contains a symlink: {path}")
    if path.is_file():
        return path
    if path.is_dir():
        return None
    raise GridOperatorError(f"project source contains a non-file: {path}")


def _is_project_cache_file(path: Path, relative: Path) -> bool:
    if "__pycache__" in relative.parts:
        return True
    return path.suffix.lower() in {".pyc", ".pyo"}


def _copy_project(project_root: Path, destination: Path) -> None:
    for source in _project_files(project_root):
        _copy_regular_file(source, destination / source.relative_to(project_root))


def _copy_source_shard(
    snapshot: SnapshotManifest, source_dir: Path, destination: Path, shard: str
) -> None:
    try:
        source_path = source_path_for(snapshot, source_dir, shard)
    except SnapshotError as error:
        raise GridOperatorError(str(error)) from error
    _copy_regular_file(source_path, destination / Path(shard))


def _copy_snapshot(run_dir: Path, destination: Path) -> None:
    _copy_regular_file(run_dir / SNAPSHOT_FILENAME, destination / SNAPSHOT_FILENAME)


def _copy_resume_state(run_dir: Path, destination: Path, shard: str) -> None:
    source_paths = shard_paths(run_dir, shard)
    if source_paths.checkpoint.is_symlink():
        raise GridOperatorError("resume checkpoint must not be a symlink")
    if not source_paths.checkpoint.is_file():
        return
    _validate_resume_state(run_dir, shard)
    target = shards_root(destination) / source_paths.root.name
    checkpoint = read_checkpoint(source_paths.checkpoint)
    _copy_regular_file(source_paths.checkpoint, target / source_paths.checkpoint.name)
    for part_name in checkpoint.completed_parts:
        _copy_regular_file(source_paths.part(part_name), target / "parts" / part_name)
        receipt = source_paths.receipt(part_name)
        _copy_regular_file(receipt, target / "receipts" / receipt.name)


def _validate_resume_state(run_dir: Path, shard: str) -> None:
    state = shard_paths(run_dir, shard)
    _validate_resume_root(state)
    if not _resume_checkpoint_is_present(state):
        return
    _validate_resume_layout(state)
    _validate_resume_report(collect_results(run_dir, shard))


def _validate_resume_root(state: ShardPaths) -> None:
    if state.root.is_symlink():
        raise GridOperatorError("resume state directory must not be a symlink")
    if state.root.exists() and not state.root.is_dir():
        raise GridOperatorError("resume state path must be a directory")


def _resume_checkpoint_is_present(state: ShardPaths) -> bool:
    if _shard_checkpoint(state) is not None:
        return True
    if _resume_root_has_children(state):
        raise GridOperatorError("resume artifacts exist without a checkpoint")
    return False


def _resume_root_has_children(state: ShardPaths) -> bool:
    return state.root.exists() and any(state.root.iterdir())


def _validate_resume_layout(state: ShardPaths) -> None:
    allowed = {
        state.checkpoint.name,
        state.parts.name,
        state.receipts.name,
        QUARANTINE_DIRNAME,
    }
    for child in state.root.iterdir():
        if child.name not in allowed:
            raise GridOperatorError(f"resume state contains an unexpected artifact: {child.name}")
        if child.name != state.checkpoint.name:
            _require_resume_directory(child)


def _require_resume_directory(path: Path) -> None:
    if path.is_symlink() or not path.is_dir():
        raise GridOperatorError(f"resume state directory is invalid: {path}")


def _validate_resume_report(report: RunReport) -> None:
    if report.issues:
        raise GridOperatorError("resume state failed collection validation")
    selected = report.shards[0]
    if selected.issues:
        raise GridOperatorError("resume state failed collection validation")


def _resume_files(root: Path) -> tuple[StagedFile, ...]:
    if not root.exists():
        return ()
    quarantine = root / QUARANTINE_DIRNAME
    files: list[StagedFile] = []
    for path in sorted(root.rglob("*"), key=lambda item: item.relative_to(root).as_posix()):
        if quarantine in path.parents:
            continue
        descriptor = _resume_file_descriptor(path, root)
        if descriptor is not None:
            files.append(descriptor)
    return tuple(files)


def _resume_file_descriptor(path: Path, root: Path) -> StagedFile | None:
    if path.is_symlink():
        raise GridOperatorError(f"resume state contains a symlink: {path}")
    if path.is_file():
        return StagedFile(
            relative_path=path.relative_to(root).as_posix(),
            size_bytes=path.stat().st_size,
            sha256=file_sha256(path),
        )
    if path.is_dir():
        return None
    raise GridOperatorError(f"resume state contains a non-file: {path}")


def _resume_state_fingerprint(run_dir: Path, shard: str) -> str:
    _validate_resume_state(run_dir, shard)
    payload = {
        "shard": shard,
        "files": [
            descriptor.to_payload()
            for descriptor in _resume_files(shard_paths(run_dir, shard).root)
        ],
    }
    return hashlib.sha256(canonical_json_bytes(payload)).hexdigest()


def _staged_resume_fingerprint(payload_root: Path) -> str | None:
    stage_path = payload_root / STAGE_MANIFEST_FILENAME
    if stage_path.is_symlink() or not stage_path.is_file():
        return None
    value = _read_staged_resume_field(stage_path)
    return value if isinstance(value, str) and _FINGERPRINT_PATTERN.fullmatch(value) else None


def _read_staged_resume_field(stage_path: Path) -> object:
    """Return the staged resume fingerprint field, or ``None`` if there is no manifest.

    A manifest that cannot be read or is not an object means "nothing staged to
    reuse", and that answer carries no message, so the read is done here rather
    than through ``_read_json``'s labelled refusal. A manifest that *is* an
    object still has to carry the field, and that refusal does reach the caller.
    """
    try:
        payload = json.loads(stage_path.read_text(encoding="utf-8"))  # pragma: no mutate - alias
        reader = require_object(payload, error=GridOperatorError, label="stage")
    except (OSError, UnicodeError, json.JSONDecodeError, GridOperatorError):
        return None
    return reader.raw("resume_state_fingerprint")


def _payload_files(root: Path) -> tuple[Path, ...]:
    if root.is_symlink() or not root.is_dir():
        raise GridOperatorError(f"portable payload root is not a regular directory: {root}")
    files: list[Path] = []
    for path in sorted(root.rglob("*"), key=lambda item: item.relative_to(root).as_posix()):
        file_path = _payload_file_descriptor(path)
        if file_path is not None:
            files.append(file_path)
    return tuple(files)


def _payload_file_descriptor(path: Path) -> Path | None:
    if path.is_symlink():
        raise GridOperatorError(f"portable payload contains a symlink: {path}")
    if path.is_file():
        return path
    if path.is_dir():
        return None
    raise GridOperatorError(f"portable payload contains a non-file: {path}")


def _write_stage_manifest(root: Path, bundle: JobBundle, resume_fingerprint: str) -> None:
    files = tuple(
        StagedFile(
            relative_path=path.relative_to(root).as_posix(),
            size_bytes=path.stat().st_size,
            sha256=file_sha256(path),
        ).to_payload()
        for path in _payload_files(root)
    )
    atomic_write_json(
        root / STAGE_MANIFEST_FILENAME,
        {
            "stage_schema_version": STAGE_SCHEMA_VERSION,
            "bundle_id": bundle.bundle_id,
            "resume_state_fingerprint": resume_fingerprint,
            "files": list(files),
        },
    )


def verify_prepared_bundle(
    payload_root: Path, *, expected_bundle: JobBundle | None = None
) -> JobBundle:
    """Verify every staged byte and identity before a compute job can run."""
    files = _payload_files(payload_root)
    bundle = _read_expected_bundle(payload_root, expected_bundle)
    stage_path, stage = _read_stage_manifest(payload_root, bundle)
    descriptors, resume_fingerprint = _stage_descriptors(stage)
    listed = _verify_stage_file_listing(payload_root, files, stage_path, descriptors)
    _verify_required_payload(payload_root, bundle, listed)
    _verify_resume_binding(payload_root, bundle, resume_fingerprint)
    return bundle


def _read_expected_bundle(payload_root: Path, expected_bundle: JobBundle | None) -> JobBundle:
    bundle = read_bundle(payload_root / BUNDLE_FILENAME)
    if expected_bundle is not None and bundle != expected_bundle:
        raise GridOperatorError("staged bundle does not match the expected bundle")
    return bundle


def _read_stage_manifest(payload_root: Path, bundle: JobBundle) -> tuple[Path, PayloadReader]:
    stage_path = payload_root / STAGE_MANIFEST_FILENAME
    if stage_path.is_symlink() or not stage_path.is_file():
        raise GridOperatorError("portable payload is missing its stage manifest")
    stage = require_object(
        _read_json(stage_path, "stage manifest"), error=GridOperatorError, label="stage"
    )
    if stage.integer("stage_schema_version") != STAGE_SCHEMA_VERSION:
        raise GridOperatorError("unsupported stage schema version")
    if stage.text("bundle_id") != bundle.bundle_id:
        raise GridOperatorError("stage manifest bundle id does not match bundle")
    return stage_path, stage


def _stage_descriptors(stage: PayloadReader) -> tuple[tuple[StagedFile, ...], str]:
    resume_fingerprint = stage.text("resume_state_fingerprint")
    _validate_fingerprint(resume_fingerprint, "resume state fingerprint")
    descriptors = tuple(StagedFile.from_payload(item) for item in stage.items("files"))
    return descriptors, resume_fingerprint


def _verify_stage_file_listing(
    payload_root: Path,
    files: tuple[Path, ...],
    stage_path: Path,
    descriptors: tuple[StagedFile, ...],
) -> set[str]:
    listed = _stage_descriptor_names(descriptors)
    actual = _payload_file_names(payload_root, files, stage_path)
    _verify_stage_file_names(actual, listed)
    _verify_staged_files(payload_root, descriptors)
    return set(listed)


def _stage_descriptor_names(descriptors: tuple[StagedFile, ...]) -> tuple[str, ...]:
    return tuple(item.relative_path for item in descriptors)


def _payload_file_names(
    payload_root: Path, files: tuple[Path, ...], stage_path: Path
) -> tuple[str, ...]:
    return tuple(path.relative_to(payload_root).as_posix() for path in files if path != stage_path)


def _verify_stage_file_names(actual: tuple[str, ...], listed: tuple[str, ...]) -> None:
    if len(set(listed)) != len(listed):
        raise GridOperatorError("stage manifest contains duplicate files")
    if set(actual) != set(listed):
        raise GridOperatorError("stage manifest does not account for every payload file")


def _verify_staged_files(payload_root: Path, descriptors: tuple[StagedFile, ...]) -> None:
    for descriptor in descriptors:
        _verify_staged_file(payload_root, descriptor)


def _verify_staged_file(payload_root: Path, descriptor: StagedFile) -> None:
    path = _payload_file(payload_root, descriptor.relative_path)
    if path.stat().st_size != descriptor.size_bytes or file_sha256(path) != descriptor.sha256:
        raise GridOperatorError(f"staged file hash does not match: {descriptor.relative_path}")


def _verify_resume_binding(payload_root: Path, bundle: JobBundle, expected: str) -> None:
    actual = _resume_state_fingerprint(payload_root / STAGE_RUN_DIRNAME, bundle.shard)
    if actual != expected:
        raise GridOperatorError("stage manifest resume state fingerprint does not match payload")


def _payload_file(root: Path, relative_path: str) -> Path:
    normalized = _validate_stage_relative_file(relative_path)
    current = root
    for part in Path(normalized).parts:
        current /= part
        if current.is_symlink():
            raise GridOperatorError(f"staged path contains a symlink: {relative_path}")
    if not current.is_file():
        raise GridOperatorError(f"staged file is missing: {relative_path}")
    return current


def _verify_required_payload(payload_root: Path, bundle: JobBundle, listed: set[str]) -> None:
    _require_payload_files(bundle, listed)
    project_root, source_root, run_root = _payload_directories(payload_root)
    _verify_payload_layout(payload_root, project_root, bundle)
    _verify_payload_identity(project_root, source_root, run_root, bundle)
    _verify_one_staged_input(source_root, bundle)
    _verify_staged_resume(run_root, bundle.shard)


def _require_payload_files(bundle: JobBundle, listed: set[str]) -> None:
    required = {
        BUNDLE_FILENAME,
        JOB_CONFIG_FILENAME,
        JOB_SCRIPT_FILENAME,
        f"{STAGE_PROJECT_DIRNAME}/pyproject.toml",
        f"{STAGE_PROJECT_DIRNAME}/uv.lock",
        f"{STAGE_SOURCE_DIRNAME}/{bundle.shard}",
        f"{STAGE_RUN_DIRNAME}/{SNAPSHOT_FILENAME}",
    }
    if not required.issubset(listed):
        missing = sorted(required - listed)
        raise GridOperatorError(f"portable payload is missing required files: {', '.join(missing)}")


def _payload_directories(payload_root: Path) -> tuple[Path, Path, Path]:
    directories = (
        payload_root / STAGE_PROJECT_DIRNAME,
        payload_root / STAGE_SOURCE_DIRNAME,
        payload_root / STAGE_RUN_DIRNAME,
    )
    for directory in directories:
        if directory.is_symlink() or not directory.is_dir():
            raise GridOperatorError(f"portable payload directory is invalid: {directory}")
    return directories


def _verify_payload_layout(payload_root: Path, project_root: Path, bundle: JobBundle) -> None:
    source_root = _project_source_root(project_root)
    if source_root.is_symlink() or not source_root.is_dir():
        raise GridOperatorError("portable payload is missing project source code")
    if not os.access(payload_root / JOB_SCRIPT_FILENAME, os.X_OK):
        raise GridOperatorError("portable job script is not executable")
    _read_job_config(payload_root / JOB_CONFIG_FILENAME, bundle)


def _verify_payload_identity(
    project_root: Path, source_root: Path, run_root: Path, bundle: JobBundle
) -> None:
    try:
        staged_snapshot = read_snapshot(run_root)
        _validate_staged_snapshot(staged_snapshot, bundle)
        verify_source_file(staged_snapshot, source_root, bundle.shard)
        _validate_staged_project(project_root, bundle)
    except SnapshotError as error:
        raise GridOperatorError(str(error)) from error


def _validate_staged_snapshot(snapshot: SnapshotManifest, bundle: JobBundle) -> None:
    if snapshot.snapshot_id != bundle.snapshot_id:
        raise GridOperatorError("staged snapshot id does not match bundle")
    if snapshot.code_fingerprint != bundle.code_fingerprint:
        raise GridOperatorError("staged snapshot code fingerprint does not match bundle")
    if snapshot.lock_fingerprint != bundle.lock_fingerprint:
        raise GridOperatorError("staged snapshot lock fingerprint does not match bundle")


def _validate_staged_project(project_root: Path, bundle: JobBundle) -> None:
    if fingerprint_project_source(project_root) != bundle.code_fingerprint:
        raise GridOperatorError("staged project source does not match bundle")
    if fingerprint_lockfile(project_root) != bundle.lock_fingerprint:
        raise GridOperatorError("staged lockfile does not match bundle")


def _verify_one_staged_input(source_root: Path, bundle: JobBundle) -> None:
    source_files = tuple(_payload_files(source_root))
    if tuple(path.relative_to(source_root).as_posix() for path in source_files) != (bundle.shard,):
        raise GridOperatorError("portable payload must contain exactly one input shard")


def _verify_staged_resume(run_root: Path, shard: str) -> None:
    _validate_resume_state(run_root, shard)


def _fsync_directory(path: Path) -> None:
    try:
        descriptor = os.open(path, os.O_RDONLY)
        try:
            os.fsync(descriptor)
        finally:
            os.close(descriptor)
    except OSError as error:
        raise GridOperatorError(f"cannot fsync directory {path}: {error}") from error


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


def import_retrieved_results(local_run_dir: Path, retrieved_run_dir: Path, shard: str) -> RunReport:
    """Validate retrieved state and commit its checkpoint last under both locks."""
    normalized_shard = _validate_relative_parquet(shard, "shard")
    _require_regular_directory(retrieved_run_dir, "retrieved run directory")
    with submission_lock(local_run_dir), exclusive_worker_lock(local_run_dir):
        return _import_retrieved_results(local_run_dir, retrieved_run_dir, normalized_shard)


def _import_retrieved_results(
    local_run_dir: Path, retrieved_run_dir: Path, normalized_shard: str
) -> RunReport:
    _require_matching_snapshots(local_run_dir, retrieved_run_dir)
    incoming_state = shard_paths(retrieved_run_dir, normalized_shard).root
    target = shard_paths(local_run_dir, normalized_shard).root
    _reject_overlapping_paths(incoming_state, target)
    with _staged_collection(local_run_dir, incoming_state, normalized_shard) as staged:
        incoming_report = collect_results(staged, normalized_shard)
        if incoming_report.shards[0].issues:
            raise GridOperatorError(
                f"retrieved {normalized_shard} has collection validation issues"
            )
        local_paths = shard_paths(local_run_dir, normalized_shard)
        staged_paths = shard_paths(staged, normalized_shard)
        first_new_part = _require_forward_progress(local_paths, staged_paths)
        _commit_retrieved_state(local_paths, staged_paths, first_new_part)
    report = collect_results(local_run_dir, normalized_shard)
    _require_clean_import_report(report)
    return report


def _require_forward_progress(local: ShardPaths, incoming: ShardPaths) -> int:
    if not local.checkpoint.exists() and not local.checkpoint.is_symlink():
        return 0
    previous = read_checkpoint(local.checkpoint)
    proposed = read_checkpoint(incoming.checkpoint)
    if proposed.input_cursor < previous.input_cursor:
        raise GridOperatorError("retrieved checkpoint would regress local progress")
    _require_checkpoint_extension(previous, proposed)
    _require_matching_committed_history(local, incoming, previous)
    return len(previous.completed_parts)


def _checkpoint_binding(checkpoint: ShardCheckpoint) -> tuple[str, str, str, int, int]:
    return (
        checkpoint.snapshot_id,
        checkpoint.model_config_fingerprint,
        checkpoint.shard,
        checkpoint.batch_size,
        checkpoint.input_row_count,
    )


def _require_checkpoint_extension(previous: ShardCheckpoint, proposed: ShardCheckpoint) -> None:
    if _checkpoint_binding(previous) != _checkpoint_binding(proposed):
        raise GridOperatorError("retrieved checkpoint changes the committed history binding")


def _require_matching_committed_history(
    local: ShardPaths, incoming: ShardPaths, previous: ShardCheckpoint
) -> None:
    count = 0
    for part in previous.completed_parts:
        _require_identical_artifact(local.part(part), incoming.part(part))
        _require_identical_artifact(local.receipt(part), incoming.receipt(part))
        count += read_receipt(incoming.receipt(part)).row_count
    if count != previous.annotation_count:
        raise GridOperatorError("local annotation count differs from committed history")


def _require_identical_artifact(local: Path, incoming: Path) -> None:
    if local.is_symlink() or not local.is_file():
        raise GridOperatorError(f"local committed history artifact is missing or unsafe: {local}")
    if file_sha256(local) != file_sha256(incoming):
        raise GridOperatorError(f"retrieved results conflict with committed history: {local}")


def _require_matching_snapshots(local_run_dir: Path, retrieved_run_dir: Path) -> None:
    try:
        local_snapshot = read_snapshot(local_run_dir)
        retrieved_snapshot = read_snapshot(retrieved_run_dir)
    except SnapshotError as error:
        raise GridOperatorError(str(error)) from error
    if local_snapshot != retrieved_snapshot:
        raise GridOperatorError("retrieved results belong to a different snapshot")


def _require_clean_import_report(report: RunReport) -> None:
    if report.issues or any(shard_report.issues for shard_report in report.shards):
        raise GridOperatorError("imported results failed local collection validation")


def _require_regular_directory(path: Path, label: str) -> None:
    if path.is_symlink() or not path.is_dir():
        raise GridOperatorError(f"{label} is not a regular directory: {path}")


def _reject_overlapping_paths(source: Path, destination: Path) -> None:
    try:
        source_resolved = source.resolve(strict=True)
    except OSError as error:
        raise GridOperatorError(f"retrieved shard directory is unavailable: {source}") from error
    destination_resolved = destination.resolve()
    if (
        source_resolved == destination_resolved
        or source_resolved.is_relative_to(destination_resolved)
        or destination_resolved.is_relative_to(source_resolved)
    ):
        raise GridOperatorError("retrieved and local shard directories must be distinct")


@contextmanager
def _staged_collection(local_run: Path, source: Path, shard: str) -> Iterator[Path]:
    with tempfile.TemporaryDirectory(prefix=".collection-", dir=local_run) as temporary:
        staged = Path(temporary)
        _copy_snapshot(local_run, staged)
        _copy_tree_no_symlinks(source, shard_paths(staged, shard).root)
        yield staged


def _commit_retrieved_state(local: ShardPaths, staged: ShardPaths, first_new_part: int) -> None:
    _prepare_collection_directories(local)
    checkpoint = read_checkpoint(staged.checkpoint)
    for part in checkpoint.completed_parts[first_new_part:]:
        _commit_staged_artifact(staged.part(part), local.part(part))
        _commit_staged_artifact(staged.receipt(part), local.receipt(part))
    atomic_write_bytes(local.checkpoint, staged.checkpoint.read_bytes())


def _prepare_collection_directories(paths: ShardPaths) -> None:
    for directory in (paths.root, paths.parts, paths.receipts):
        if directory.is_symlink():
            raise GridOperatorError(f"local result directory must not be a symlink: {directory}")
        directory.mkdir(parents=True, exist_ok=True)


def _commit_staged_artifact(source: Path, destination: Path) -> None:
    atomic_write_via(destination, lambda temporary: os.replace(source, temporary))


def _copy_tree_no_symlinks(source: Path, destination: Path) -> None:
    _require_regular_directory(source, "source directory")
    destination.mkdir(parents=True, exist_ok=True)
    for path in sorted(source.iterdir()):
        _copy_tree_entry(path, destination / path.name)


def _copy_tree_entry(source: Path, destination: Path) -> None:
    if source.is_symlink():
        raise GridOperatorError(f"retrieved results contain a symlink: {source}")
    if source.is_dir():
        _copy_tree_no_symlinks(source, destination)
    elif source.is_file():
        _copy_regular_file(source, destination)
    else:
        raise GridOperatorError(f"retrieved results contain a non-file: {source}")


def acknowledge_collected_results(
    paths: JobPaths, report: RunReport, *, now: datetime | None = None
) -> SubmissionIntent:
    """Durably acknowledge one validated terminal result for a shard."""
    selected = _acknowledgment_shard(report)
    with submission_lock(paths.run_dir):
        intent, bundle = _bound_submission(paths)
        _validate_acknowledgment(report, selected, intent, bundle)
        updated = _acknowledge_intent(intent, report, now)
        atomic_write_json(paths.intent, updated.to_payload())
        return updated


def _acknowledgment_shard(report: RunReport) -> ShardReport:
    if not isinstance(report, RunReport) or len(report.shards) != 1:
        raise GridOperatorError("collection acknowledgment requires one shard report")
    return report.shards[0]


def _bound_submission(paths: JobPaths) -> tuple[SubmissionIntent, JobBundle]:
    intent = read_intent(paths.intent)
    bundle = _existing_bundle(paths)
    if bundle is None:
        raise GridOperatorError("submission intent is not bound to the prepared bundle")
    if intent.bundle_id != bundle.bundle_id or intent.shard != bundle.shard:
        raise GridOperatorError("submission intent is not bound to the prepared bundle")
    return intent, bundle


def _validate_acknowledgment(
    report: RunReport,
    selected: ShardReport,
    intent: SubmissionIntent,
    bundle: JobBundle,
) -> None:
    _validate_ack_identity(report, selected, bundle)
    _validate_ack_report_clean(report, selected)
    _validate_ack_terminal(selected, intent)
    if intent.result_complete and not report.is_complete:
        raise GridOperatorError("complete results cannot be downgraded to paused results")


def _validate_ack_identity(report: RunReport, selected: ShardReport, bundle: JobBundle) -> None:
    if report.snapshot_id != bundle.snapshot_id:
        raise GridOperatorError("collected results do not match the submitted bundle")
    if selected.shard != bundle.shard:
        raise GridOperatorError("collected results do not match the submitted bundle")


def _validate_ack_report_clean(report: RunReport, selected: ShardReport) -> None:
    if report.issues or selected.issues:
        raise GridOperatorError("cannot acknowledge collection validation issues")


def _validate_ack_terminal(selected: ShardReport, intent: SubmissionIntent) -> None:
    if selected.status not in {str(ShardStatus.PAUSED), str(ShardStatus.COMPLETE)}:
        raise GridOperatorError("collected results do not have a terminal checkpoint")
    if intent.terminal_state != str(JobState.TERMINATED):
        raise GridOperatorError("collected results require a terminal scheduler state")


def _acknowledge_intent(
    intent: SubmissionIntent, report: RunReport, now: datetime | None
) -> SubmissionIntent:
    return replace(
        intent,
        result_acknowledged=True,
        result_complete=report.is_complete,
        collected_at=(now or datetime.now(UTC)).isoformat(),
    )


@dataclass(frozen=True, slots=True)
class SubmissionPlan:
    """Exactly what a submission would do, before any gate is opened."""

    bundle_id: str
    shard: str
    job_name: str
    walltime_seconds: int
    cores: int
    argv: tuple[str, ...]
    policy: PolicyVerdict
    attempt: int
    blocked_reason: str | None

    @property
    def may_apply(self) -> bool:
        """Return whether an apply gate would be honoured."""
        return self.blocked_reason is None and self.policy.may_submit

    def to_payload(self) -> dict[str, object]:
        return {
            "bundle_id": self.bundle_id,
            "shard": self.shard,
            "job_name": self.job_name,
            "walltime_seconds": self.walltime_seconds,
            "cores": self.cores,
            "argv": list(self.argv),
            "policy": self.policy.to_payload(),
            "attempt": self.attempt,
            "may_apply": self.may_apply,
            "blocked_reason": self.blocked_reason,
        }


def _existing_intent_block(
    paths: JobPaths, *, max_attempts: int | None = None
) -> tuple[str | None, int]:
    _validate_max_attempts(max_attempts)
    if not paths.intent.is_file():
        return None, 1
    intent = read_intent(paths.intent)
    blocked_reason = _intent_retry_block(intent, max_attempts)
    if blocked_reason is not None:
        return blocked_reason, intent.attempt
    return None, intent.attempt + 1


def _validate_max_attempts(value: int | None) -> None:
    if value is not None and (type(value) is not int or value < 1):
        raise GridOperatorError("maximum submission attempts must be a positive integer or null")


def _intent_retry_block(intent: SubmissionIntent, max_attempts: int | None) -> str | None:
    if intent.outcome == str(SubmissionOutcome.REJECTED):
        return _attempt_limit_reason(intent, max_attempts)
    if intent.is_active_or_ambiguous:
        return _active_intent_reason(intent)
    if intent.result_complete:
        return "complete results have already been acknowledged for this shard"
    if not intent.result_acknowledged:
        return "terminal job results must be collected and acknowledged before retrying"
    return _attempt_limit_reason(intent, max_attempts)


def _attempt_limit_reason(intent: SubmissionIntent, max_attempts: int | None) -> str | None:
    if max_attempts is not None and intent.attempt >= max_attempts:
        return f"maximum of {max_attempts} attempts has been reached for this shard"
    return None


def _active_intent_reason(intent: SubmissionIntent) -> str:
    if intent.job_id is not None:
        return f"job {intent.job_id} was already submitted for this bundle; reconcile it first"
    return (
        "a previous submission left an unresolved intent; "
        "reconcile whether a job exists before submitting again"
    )


def _run_wide_intent_block(paths: JobPaths, bundle: JobBundle) -> str | None:
    for _candidate, intent in _iter_intents(paths.run_dir):
        # ``_iter_intents`` refuses an intent that does not bind to its own directory's
        # bundle, and a job directory is named after its bundle, so this also skips
        # this job's own intent file.
        if intent.bundle_id == bundle.bundle_id:
            continue
        if intent.is_active_or_ambiguous:
            return (
                f"another shard has an active or ambiguous submission "
                f"({intent.shard}, bundle {intent.bundle_id[:16]}); reconcile it first"
            )
    return None


def _iter_intents(run_dir: Path) -> Iterator[tuple[Path, SubmissionIntent]]:
    jobs_root = run_dir / "jobs"
    if not jobs_root.exists():
        return
    _require_jobs_root(jobs_root)
    for candidate in sorted(jobs_root.iterdir()):
        _require_job_directory(candidate)
        found = _read_intent_candidate(candidate)
        if found is not None:
            yield found


def _require_jobs_root(jobs_root: Path) -> None:
    if jobs_root.is_symlink() or not jobs_root.is_dir():
        raise GridOperatorError(f"jobs directory is not a regular directory: {jobs_root}")


def _require_job_directory(candidate: Path) -> None:
    if candidate.is_symlink() or not candidate.is_dir():
        raise GridOperatorError(f"job directory must be a regular directory: {candidate}")


def _read_intent_candidate(candidate: Path) -> tuple[Path, SubmissionIntent] | None:
    intent_path = _intent_file_if_present(candidate)
    if intent_path is None:
        return None
    intent = read_intent(intent_path)
    candidate_bundle = _candidate_bundle(candidate, intent_path)
    _verify_intent_binding(candidate_bundle, intent, intent_path)
    return intent_path, intent


def _intent_file_if_present(candidate: Path) -> Path | None:
    intent_path = candidate / INTENT_FILENAME
    if intent_path.is_symlink():
        raise GridOperatorError(f"submission intent must be a regular file: {intent_path}")
    if not intent_path.exists():
        return None
    if not intent_path.is_file():
        raise GridOperatorError(f"submission intent must be a regular file: {intent_path}")
    return intent_path


def _verify_intent_binding(
    candidate_bundle: JobBundle, intent: SubmissionIntent, intent_path: Path
) -> None:
    if candidate_bundle.bundle_id != intent.bundle_id:
        raise GridOperatorError(f"submission intent is not bound to its bundle: {intent_path}")
    if candidate_bundle.shard != intent.shard:
        raise GridOperatorError(f"submission intent is not bound to its bundle: {intent_path}")


def _candidate_bundle(candidate: Path, intent_path: Path) -> JobBundle:
    bundle_path = candidate / BUNDLE_FILENAME
    if bundle_path.is_symlink() or not bundle_path.is_file():
        raise GridOperatorError(f"submission intent has no bound bundle: {intent_path}")
    return read_bundle(bundle_path)


@contextmanager
def submission_lock(run_dir: Path) -> Iterator[None]:
    """Hold the run-wide non-blocking lock around submission state changes."""
    _require_run_directory(run_dir)
    run_dir.mkdir(parents=True, exist_ok=True)
    lock_path = run_dir / GRID_SUBMISSION_LOCK_FILENAME
    _require_lock_path(lock_path)
    handle = _open_lock(lock_path)
    try:
        _acquire_lock(handle, run_dir)
        try:
            yield
        finally:
            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
    finally:
        handle.close()


def _require_run_directory(run_dir: Path) -> None:
    if run_dir.is_symlink():
        raise GridOperatorError(f"run directory is not a regular directory: {run_dir}")
    if run_dir.exists() and not run_dir.is_dir():
        raise GridOperatorError(f"run directory is not a regular directory: {run_dir}")


def _require_lock_path(lock_path: Path) -> None:
    if lock_path.is_symlink():
        raise GridOperatorError(f"submission lock must be a regular file: {lock_path}")
    if lock_path.exists() and not lock_path.is_file():
        raise GridOperatorError(f"submission lock must be a regular file: {lock_path}")


def _open_lock(lock_path: Path) -> TextIO:
    try:
        return lock_path.open("a+")
    except OSError as error:
        raise GridOperatorError(f"cannot open submission lock {lock_path}: {error}") from error


def _acquire_lock(handle: TextIO, run_dir: Path) -> None:
    try:
        fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError as error:
        if error.errno in {errno.EACCES, errno.EAGAIN}:
            raise GridOperatorError(f"submission lock is already held: {run_dir}") from error
        raise GridOperatorError(f"cannot acquire submission lock {run_dir}: {error}") from error


def _policy_freshness_block(policy: PolicyVerdict, moment: datetime) -> str | None:
    captured_at = policy.evidence.captured_at
    if captured_at is None:
        return "policy evidence freshness is unknown"
    try:
        fresh = policy_evidence_is_fresh(moment, captured_at)
    except ValueError:
        return "policy evidence freshness is unknown"
    return None if fresh else "policy evidence is stale or from the future"


def plan_submission(
    paths: JobPaths,
    bundle: JobBundle,
    *,
    policy: PolicyVerdict,
    allowed_root: Path,
    walltime_seconds: int = MAX_WALLTIME_SECONDS,
    night_noretry: bool = True,
    max_attempts: int | None = None,
    require_fresh_policy: bool = False,
    now: datetime | None = None,
) -> SubmissionPlan:
    """Describe the submission without contacting the scheduler."""
    _validate_prepared_submission(paths, bundle, walltime_seconds)
    request = _submission_request(bundle, walltime_seconds, night_noretry, paths)
    blocked_reason, attempt = _submission_block(
        paths,
        bundle,
        max_attempts,
        require_fresh_policy,
        policy,
        now,
    )
    return SubmissionPlan(
        bundle_id=bundle.bundle_id,
        shard=bundle.shard,
        job_name=request.name,
        walltime_seconds=request.walltime_seconds,
        cores=request.cores,
        argv=build_oarsub_argv(request, allowed_root=allowed_root),
        policy=policy,
        attempt=attempt,
        blocked_reason=blocked_reason,
    )


def _validate_prepared_submission(
    paths: JobPaths, bundle: JobBundle, walltime_seconds: int
) -> None:
    _validate_submission_walltime(walltime_seconds)
    stored = _existing_bundle(paths)
    if stored is None or stored != bundle:
        raise GridOperatorError("prepared bundle does not match the requested submission")
    config = _read_job_config(paths.config, bundle)
    if config["walltime_seconds"] != walltime_seconds:
        raise GridOperatorError("submission walltime differs from prepared job config")


def _validate_submission_walltime(value: object) -> None:
    if type(value) is not int or value <= 0:
        raise GridOperatorError("walltime must be a positive number of seconds for this workload")
    if value > MAX_WALLTIME_SECONDS:
        raise GridOperatorError(
            f"walltime must not exceed {MAX_WALLTIME_SECONDS} seconds for this workload"
        )


def _submission_block(
    paths: JobPaths,
    bundle: JobBundle,
    max_attempts: int | None,
    require_fresh_policy: bool,
    policy: PolicyVerdict,
    now: datetime | None,
) -> tuple[str | None, int]:
    blocked_reason, attempt = _existing_intent_block(paths, max_attempts=max_attempts)
    if blocked_reason is None:
        blocked_reason = _run_wide_intent_block(paths, bundle)
    if blocked_reason is None:
        blocked_reason = _shard_state_block(paths, bundle, require_fresh_policy, policy, now)
    return blocked_reason, attempt


def _shard_state_block(
    paths: JobPaths,
    bundle: JobBundle,
    require_fresh_policy: bool,
    policy: PolicyVerdict,
    now: datetime | None,
) -> str | None:
    """Block on the shard's own state, then on policy freshness if required."""
    blocked_reason = _initial_checkpoint_block(paths, bundle)
    if blocked_reason is None and require_fresh_policy:
        blocked_reason = _policy_freshness_block(policy, now or datetime.now(UTC))
    return blocked_reason


def _initial_checkpoint_block(paths: JobPaths, bundle: JobBundle) -> str | None:
    try:
        checkpoint = _shard_checkpoint(shard_paths(paths.run_dir, bundle.shard))
    except GridOperatorError as error:
        return str(error)
    if checkpoint is None:
        return "no initial shard checkpoint exists; prepare this job again before submitting"
    try:
        _require_bound_checkpoint(checkpoint, bundle)
    except GridOperatorError as error:
        return str(error)
    return None


def _submission_request(
    bundle: JobBundle,
    walltime_seconds: int,
    night_noretry: bool,
    paths: JobPaths,
) -> SubmissionRequest:
    """Build the request; the walltime is validated before this is reached."""
    return SubmissionRequest(
        script=paths.script,
        walltime_seconds=walltime_seconds,
        cores=REQUIRED_CORES,
        name=f"lang-{bundle.bundle_id[:16]}",
        night_noretry=night_noretry,
    )


def submit_job(
    paths: JobPaths,
    bundle: JobBundle,
    *,
    policy: PolicyVerdict,
    allowed_root: Path,
    apply: bool = False,
    walltime_seconds: int = MAX_WALLTIME_SECONDS,
    night_noretry: bool = True,
    max_attempts: int | None = None,
    require_fresh_policy: bool = False,
    runner: CommandRunner = run_command,
    now: datetime | None = None,
) -> tuple[SubmissionPlan, SubmissionResult | None]:
    """Submit one job only behind an explicit gate and a passing policy verdict.

    The intent is written before ``oarsub`` runs, so an ambiguous scheduler
    response still leaves durable evidence that a job may exist.
    """
    if not apply:
        return (
            plan_submission(
                paths,
                bundle,
                policy=policy,
                allowed_root=allowed_root,
                walltime_seconds=walltime_seconds,
                night_noretry=night_noretry,
                max_attempts=max_attempts,
                require_fresh_policy=require_fresh_policy,
                now=now,
            ),
            None,
        )
    with submission_lock(paths.run_dir):
        plan = plan_submission(
            paths,
            bundle,
            policy=policy,
            allowed_root=allowed_root,
            walltime_seconds=walltime_seconds,
            night_noretry=night_noretry,
            max_attempts=max_attempts,
            require_fresh_policy=True,
            now=now,
        )
        if not plan.may_apply:
            return plan, None
        intent = SubmissionIntent(
            bundle_id=bundle.bundle_id,
            shard=bundle.shard,
            job_name=plan.job_name,
            walltime_seconds=plan.walltime_seconds,
            cores=plan.cores,
            recorded_at=(now or datetime.now(UTC)).isoformat(),
            attempt=plan.attempt,
        )
        atomic_write_json(paths.intent, intent.to_payload())
        request = _submission_request(bundle, walltime_seconds, night_noretry, paths)
        result = submit(request, allowed_root=allowed_root, runner=runner)
        _record_outcome(paths, intent, result)
        return plan, result


def _record_outcome(paths: JobPaths, intent: SubmissionIntent, result: SubmissionResult) -> None:
    atomic_write_json(
        paths.intent,
        replace(
            intent,
            job_id=result.job_id,
            outcome=str(result.outcome),
            detail=result.detail,
        ).to_payload(),
    )


@dataclass(frozen=True, slots=True)
class Reconciliation:
    """What is known about a previously recorded submission."""

    intent: SubmissionIntent | None
    state: JobState
    detail: str

    @property
    def needs_operator_attention(self) -> bool:
        """Return whether a human must resolve this before any resubmission."""
        if self.intent is None:
            return False
        return self.intent.is_unresolved or self.state is JobState.UNKNOWN

    def to_payload(self) -> dict[str, object]:
        return {
            "intent": None if self.intent is None else self.intent.to_payload(),
            "state": str(self.state),
            "detail": self.detail,
            "needs_operator_attention": self.needs_operator_attention,
        }


def reconcile_job(
    paths: JobPaths,
    *,
    apply: bool = False,
    runner: CommandRunner = run_command,
    resolve_job_name: JobNameResolver | None = None,
    now: datetime | None = None,
) -> Reconciliation:
    """Identify an existing job before any further submission is considered."""
    if resolve_job_name is not None and not callable(resolve_job_name):
        raise GridOperatorError("job-name resolver must be callable")
    if not apply:
        return _dry_reconciliation(paths)
    with submission_lock(paths.run_dir):
        return _live_reconciliation(paths, runner, resolve_job_name, now)


def _dry_reconciliation(paths: JobPaths) -> Reconciliation:
    if not paths.intent.is_file():
        return Reconciliation(None, JobState.UNKNOWN, "no submission intent has been recorded")
    intent = read_intent(paths.intent)
    if intent.terminal_state == str(JobState.TERMINATED):
        return Reconciliation(intent, JobState.TERMINATED, "terminal state was already reconciled")
    if intent.job_id is None:
        return _missing_job_id_reconciliation(intent)
    return Reconciliation(intent, JobState.UNKNOWN, "pass the apply gate to query the scheduler")


def _missing_job_id_reconciliation(intent: SubmissionIntent) -> Reconciliation:
    return Reconciliation(
        intent,
        JobState.UNKNOWN,
        "intent has no job identifier; query the scheduler by job name before resubmitting",
    )


def resolve_job_name(
    job_name: str,
    *,
    runner: CommandRunner = run_command,
    timeout: float = DEFAULT_COMMAND_TIMEOUT,
) -> int | None:
    """Resolve one recorded account job by exact name using read-only OAR JSON.

    OAR's account listing is bounded by the jobs it still retains in its
    account view, including any recent terminal records it exposes. ``None``
    is an unresolved result, never permission to resubmit; site retention
    limits can therefore require manual operator investigation.
    """
    account_jobs = _account_lookup_output(runner, timeout)
    try:
        return resolve_account_job_name(account_jobs, job_name)
    except SchedulerError as error:
        raise GridOperatorError(f"cannot resolve account job by name: {error}") from error


def _account_lookup_output(runner: CommandRunner, timeout: float) -> str:
    argv = ("oarstat", "-u", "-J")
    try:
        result = runner(argv, timeout)
    except SchedulerError as error:
        raise GridOperatorError(f"cannot query account jobs for reconciliation: {error}") from error
    if result.timed_out:
        raise GridOperatorError("account job lookup timed out; do not resubmit")
    if result.returncode != 0:
        raise GridOperatorError(f"account job lookup exited {result.returncode}; do not resubmit")
    return "{}" if result.stdout == "" else result.stdout


def _live_reconciliation(
    paths: JobPaths,
    runner: CommandRunner,
    resolve_job_name: JobNameResolver | None,
    now: datetime | None,
) -> Reconciliation:
    if not paths.intent.is_file():
        return Reconciliation(None, JobState.UNKNOWN, "no submission intent has been recorded")
    intent = read_intent(paths.intent)
    resolved = _resolved_job(paths, intent, resolve_job_name)
    if isinstance(resolved, Reconciliation):
        return resolved
    bound, job_id = resolved
    state, detail = job_state(job_id, runner=runner)
    reconciled = _record_terminal_reconciliation(paths, bound, state, now)
    return Reconciliation(reconciled, state, detail)


def _resolved_job(
    paths: JobPaths,
    intent: SubmissionIntent,
    resolver: JobNameResolver | None,
) -> tuple[SubmissionIntent, int] | Reconciliation:
    """Return the intent bound to a known job id, or why it stays unresolved."""
    if intent.job_id is not None:
        return intent, intent.job_id
    if resolver is None:
        return _missing_job_id_reconciliation(intent)
    return _resolved_by_name(paths, intent, resolver)


def _resolved_by_name(
    paths: JobPaths, intent: SubmissionIntent, resolver: JobNameResolver
) -> tuple[SubmissionIntent, int] | Reconciliation:
    resolved_id = resolver(intent.job_name)
    if resolved_id is None:
        return Reconciliation(
            intent,
            JobState.UNKNOWN,
            "scheduler lookup by job name did not resolve a job; do not resubmit",
        )
    if type(resolved_id) is not int or resolved_id < 1:
        raise GridOperatorError("job-name resolver must return a positive integer or null")
    updated = replace(intent, job_id=resolved_id)
    atomic_write_json(paths.intent, updated.to_payload())
    return updated, resolved_id


def _record_terminal_reconciliation(
    paths: JobPaths,
    intent: SubmissionIntent,
    state: JobState,
    now: datetime | None,
) -> SubmissionIntent:
    if state is not JobState.TERMINATED:
        return intent
    updated = replace(
        intent,
        terminal_state=str(JobState.TERMINATED),
        reconciled_at=(now or datetime.now(UTC)).isoformat(),
    )
    atomic_write_json(paths.intent, updated.to_payload())
    return updated


def gather_policy_outputs(
    site: str,
    *,
    runner: CommandRunner = run_command,
    timeout: float = DEFAULT_COMMAND_TIMEOUT,
) -> tuple[str | None, str | None]:
    """Capture live usage-policy and home-quota output from a frontend.

    Each command that fails, times out, or is unavailable yields ``None`` so the
    policy evaluation treats its evidence as unknown rather than as approval.
    ``quota -l`` is deliberately not used: it omits NFS home storage.
    """
    if not isinstance(site, str) or not site or not site.replace("-", "").isalnum():
        raise GridOperatorError("site must be a simple alphanumeric name")
    usage = _captured(("usagepolicycheck", "-t", "--sites", site, "--json"), runner, timeout)
    quota = _captured(("quota", "-p", "-w"), runner, timeout)
    return usage, quota


def gather_policy_evidence(
    site: str,
    *,
    runner: CommandRunner = run_command,
    timeout: float = DEFAULT_COMMAND_TIMEOUT,
) -> tuple[str | None, str | None, str | None]:
    """Capture usage, home-quota, and account-wide job-state evidence.

    The third value is ``oarstat -u -J`` output, with successful zero-byte OAR2
    responses normalized to ``{}``. Callers must parse it with
    :func:`parse_account_job_states` and inject its active count into policy
    evaluation; the historical usage-policy ``total_jobs`` is never reused.
    """
    usage, quota = gather_policy_outputs(site, runner=runner, timeout=timeout)
    account_jobs = _captured(("oarstat", "-u", "-J"), runner, timeout)
    if account_jobs == "":
        account_jobs = "{}"
    return usage, quota, account_jobs


def _captured(argv: tuple[str, ...], runner: CommandRunner, timeout: float) -> str | None:
    try:
        result = runner(argv, timeout)
    except SchedulerError:
        return None
    if result.timed_out or result.returncode != 0:
        return None
    return result.stdout


def collect_results(run_dir: Path, shard: str) -> RunReport:
    """Validate what a completed or paused job actually produced."""
    return validate_run(run_dir, shards=(shard,))


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
    "render_job_script",
    "resolve_job_name",
    "submission_lock",
    "submit_job",
    "verify_prepared_bundle",
]
