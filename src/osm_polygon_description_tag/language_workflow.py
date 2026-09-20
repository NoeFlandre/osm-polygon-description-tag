"""Local language snapshot, detection, and validation workflows."""

from __future__ import annotations

from pathlib import Path

from osm_polygon_description_tag.dataset.languages.checkpoint import exclusive_worker_lock
from osm_polygon_description_tag.dataset.languages.detector import (
    FallbackLanguageDetector,
    LanguageDetector,
    build_language_detector,
    build_lingua_detector,
)
from osm_polygon_description_tag.dataset.languages.models import (
    CASCADE_DETECTOR_NAME,
    DEFAULT_LANGUAGE_POLICY,
    DEFAULT_LANGUAGE_SCOPE,
    LINGUA_DETECTOR_NAME,
    LanguageModelIdentity,
    LanguagePolicy,
    cascade_model_identity,
)
from osm_polygon_description_tag.dataset.languages.snapshot import (
    SnapshotError,
    SnapshotManifest,
    fingerprint_lockfile,
    fingerprint_project_source,
    prepare_snapshot,
    read_snapshot,
    verify_project_identity,
)
from osm_polygon_description_tag.dataset.languages.validation import validate_run
from osm_polygon_description_tag.dataset.languages.worker import ProcessingBudget, process_shard
from osm_polygon_description_tag.dataset.sentences.sat import build_sat_splitter
from osm_polygon_description_tag.runtime.presentation import print_json

_POLICY_PRESETS = {"v1": DEFAULT_LANGUAGE_POLICY}


def _policy(
    min_alphabetic_chars: int | None,
    *,
    policy_version: str = "v1",
) -> LanguagePolicy:
    try:
        base = _POLICY_PRESETS[policy_version]
    except KeyError:
        raise ValueError("policy_version must be 'v1'") from None
    return LanguagePolicy(
        min_alphabetic_chars=(
            base.min_alphabetic_chars if min_alphabetic_chars is None else min_alphabetic_chars
        ),
    )


def _prepare_identity(
    policy: LanguagePolicy, model_identity: LanguageModelIdentity | None
) -> LanguageModelIdentity:
    identity = model_identity or cascade_model_identity(policy)
    if not isinstance(identity, LanguageModelIdentity):
        raise TypeError("model_identity must be a LanguageModelIdentity")
    if identity.policy != policy:
        raise SnapshotError("model identity policy does not match the requested policy")
    return identity


def _prepare_report(
    source_root: Path, run_dir: Path, snapshot: SnapshotManifest
) -> dict[str, object]:
    return {
        "snapshot_id": snapshot.snapshot_id,
        "run_dir": str(run_dir),
        "source_root": str(source_root),
        "source_file_count": len(snapshot.source_files),
        "input_row_count": sum(item.row_count for item in snapshot.source_files),
        "model_config_fingerprint": snapshot.model_config_fingerprint,
        "detector_name": snapshot.model_identity.detector_name,
        "library_name": snapshot.model_identity.library_name,
        "library_version": snapshot.model_identity.library_version,
        "shards": [item.relative_path for item in snapshot.source_files],
    }


def handle_prepare(
    source_root: Path,
    run_dir: Path,
    project_root: Path,
    policy: LanguagePolicy,
    *,
    model_identity: LanguageModelIdentity | None = None,
) -> SnapshotManifest:
    """Freeze the immutable input snapshot for one run directory."""
    identity = _prepare_identity(policy, model_identity)
    snapshot = prepare_snapshot(
        source_root,
        run_dir,
        code_fingerprint=fingerprint_project_source(project_root),
        lock_fingerprint=fingerprint_lockfile(project_root),
        model_identity=identity,
        policy=policy,
    )
    print_json(_prepare_report(source_root, run_dir, snapshot))
    return snapshot


def handle_run(
    source_root: Path,
    run_dir: Path,
    shard: str,
    batch_size: int,
    budget_seconds: float,
    project_root: Path = Path(),
    *,
    sat_model_path: Path,
    glotlid_model_path: Path | None = None,
) -> None:
    """Process one staged shard within a bounded monotonic budget.

    The run directory is locked for the lifetime of the attempt so a second
    local worker cannot interleave commits into the same checkpoints.
    """
    snapshot = read_snapshot(run_dir)
    with exclusive_worker_lock(run_dir):
        verify_project_identity(snapshot, project_root)
        detector = _build_detector_for_snapshot(
            snapshot.model_identity,
            glotlid_model_path=glotlid_model_path,
        )
        if detector.identity.config_fingerprint != snapshot.model_config_fingerprint:
            raise SnapshotError("detector configuration does not match the immutable snapshot")
        outcome = process_shard(
            run_dir,
            source_root,
            shard,
            detector=detector,
            splitter=build_sat_splitter(model_dir=sat_model_path),
            snapshot=snapshot,
            batch_size=batch_size,
            budget=ProcessingBudget(budget_seconds),
        )
    print_json(
        {
            "snapshot_id": snapshot.snapshot_id,
            "shard": outcome.shard,
            "status": str(outcome.status),
            "complete": outcome.is_complete,
            "resumed_from": outcome.resumed_from,
            "input_cursor": outcome.input_cursor,
            "input_row_count": outcome.input_row_count,
            "annotation_count": outcome.annotation_count,
            "part_count": len(outcome.completed_parts),
        }
    )


def handle_validate(run_dir: Path, shard: str | None) -> None:
    """Report run completeness without modifying anything."""
    report = validate_run(run_dir, shards=None if shard is None else (shard,))
    print_json({"run_dir": str(run_dir), **report.to_payload()})


def _build_detector_for_snapshot(
    identity: LanguageModelIdentity,
    *,
    glotlid_model_path: Path | None,
) -> LanguageDetector | FallbackLanguageDetector:
    scope = identity.language_scope
    language_codes = None if scope == DEFAULT_LANGUAGE_SCOPE else scope
    if identity.detector_name == CASCADE_DETECTOR_NAME:
        return build_language_detector(
            identity.policy,
            language_codes=language_codes,
            glotlid_model_path=glotlid_model_path,
        )
    if identity.detector_name == LINGUA_DETECTOR_NAME:
        if glotlid_model_path is not None:
            raise SnapshotError("GlotLID model path requires a cascade snapshot")
        return build_lingua_detector(identity.policy, language_codes=language_codes)
    raise SnapshotError(f"unsupported detector in snapshot: {identity.detector_name!r}")
