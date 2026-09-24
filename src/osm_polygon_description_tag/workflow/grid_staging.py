"""Stage and verify the portable payload that is copied to a Grid'5000 site."""

import hashlib
import json
import os
import shutil
import tempfile
from pathlib import Path

from osm_polygon_description_tag.dataset.languages.checkpoint import (
    CheckpointError,
    read_checkpoint,
    shard_paths,
    shards_root,
)
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
from osm_polygon_description_tag.dataset.manifest import file_sha256
from osm_polygon_description_tag.runtime.atomic import (
    atomic_write_json,
)
from osm_polygon_description_tag.runtime.serialization import canonical_json_bytes
from osm_polygon_description_tag.runtime.validation import FINGERPRINT_PATTERN
from osm_polygon_description_tag.workflow.grid_job_script import (
    _validate_remote_path,
)
from osm_polygon_description_tag.workflow.grid_models import (
    BUNDLE_FILENAME,
    JOB_CONFIG_FILENAME,
    JOB_SCRIPT_FILENAME,
    QUARANTINE_DIRNAME,
    STAGE_MANIFEST_FILENAME,
    STAGE_PROJECT_DIRNAME,
    STAGE_RUN_DIRNAME,
    STAGE_SCHEMA_VERSION,
    STAGE_SOURCE_DIRNAME,
    GridOperatorError,
    JobBundle,
    JobPaths,
    PreparedJob,
    StagedFile,
    _read_json,
    _validate_fingerprint,
    _validate_stage_relative_file,
    read_bundle,
    remote_child,
)
from osm_polygon_description_tag.workflow.grid_policy import (
    MAX_PROCESSING_SECONDS,
    MAX_WALLTIME_SECONDS,
)
from osm_polygon_description_tag.workflow.grid_prepare import (
    _read_job_config,
    prepare_job,
)
from osm_polygon_description_tag.workflow.grid_state import (
    _fsync_directory,
    _validate_resume_state,
    submission_lock,
)


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
    remote_project = remote_child(base, STAGE_PROJECT_DIRNAME)
    remote_source = remote_child(base, STAGE_SOURCE_DIRNAME)
    remote_run = remote_child(base, STAGE_RUN_DIRNAME)
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
    return value if isinstance(value, str) and FINGERPRINT_PATTERN.fullmatch(value) else None


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
