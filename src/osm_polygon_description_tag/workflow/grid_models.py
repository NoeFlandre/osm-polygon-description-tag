"""Identity, intent, and path models shared by every Grid'5000 operator step."""

import hashlib
import json
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from functools import partial
from pathlib import Path
from typing import Final, cast

from osm_polygon_description_tag.dataset.languages.paths import relative_posix_path
from osm_polygon_description_tag.dataset.languages.payloads import PayloadReader, require_object
from osm_polygon_description_tag.dataset.languages.snapshot import (
    SnapshotManifest,
)
from osm_polygon_description_tag.runtime.serialization import canonical_json_bytes
from osm_polygon_description_tag.runtime.validation import validate_fingerprint
from osm_polygon_description_tag.workflow.grid_scheduler import (
    JobState,
    SubmissionOutcome,
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


_validate_fingerprint = partial(validate_fingerprint, error=GridOperatorError)


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


def remote_child(base: str, name: str) -> str:
    return f"/{name}" if base == "/" else f"{base}/{name}"
