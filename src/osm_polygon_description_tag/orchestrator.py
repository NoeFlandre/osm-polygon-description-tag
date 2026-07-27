"""Stoppable, resumable per-PBF build + publish orchestrator.

The single public command (``run_and_publish``) performs a preflight,
iterates discovered sources in deterministic filename order, and for each one:

1. Reuses the existing output if its manifest agrees with the current
   source/output identity, schema versions, area-policy checksum, transform
   algorithm version, and output algorithm revision.
2. Otherwise rebuilds exactly that PBF with the real osmium binary.
3. Validates the produced GeoParquet and manifest, regenerates
   ``stats.json`` and ``README.md``, and computes a per-PBF upload plan
   containing exactly four files (Parquet, manifest, stats, README).
4. Uses :func:`publication.execute_upload` as the single publishing
   abstraction (which revalidates the plan, the manifest, and the parquet,
   verifies the remote revision through the official Hub API, and applies
   bounded retry on retryable failures).
5. Records the verified remote commit identity in
   ``publication-state.json`` atomically, but only after remote verification.

Three mutually exclusive per-source outcomes are exposed:

- ``built-needs-upload`` — a fresh parquet was produced and must be uploaded.
- ``reused-local-needs-upload`` — a valid local artifact exists but has no
  matching remote state, so it must be uploaded.
- ``already-published`` — the local artifact matches the publication state, so
  nothing must happen on disk and nothing must be uploaded.

A Ctrl-C interrupt terminates the active osmium child, removes only owned
temporary files, leaves prior artifacts intact, and exits with code 130. A
restart skips locally valid completed PBFs and previously committed uploads,
and safely retries an upload whose remote commit succeeded but whose local
checkpoint did not.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import uuid
from collections.abc import Callable, Iterable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Protocol

from osm_polygon_description_tag._resources import (
    dataset_card_template,
    osmium_export_config,
)
from osm_polygon_description_tag.config import Paths
from osm_polygon_description_tag.discovery import Source, discover_sources
from osm_polygon_description_tag.extraction import ExportRecord
from osm_polygon_description_tag.manifest import (
    Manifest,
    file_sha256,
    is_resumable,
    output_identity_for,
    read_manifest,
    source_identity_for,
)
from osm_polygon_description_tag.pipeline import build_one
from osm_polygon_description_tag.publication import (
    REPO_ID,
    PublicationError,
    UploadItem,
    UploadPlan,
    create_upload_plan,
    execute_upload,
)
from osm_polygon_description_tag.publication import (
    file_sha256_bytes as publication_file_sha256_bytes,
)
from osm_polygon_description_tag.reporting import generate_dataset_docs
from osm_polygon_description_tag.storage import StorageError, validate_geoparquet

PUBLICATION_STATE_FILENAME = "publication-state.json"
INTERRUPT_EXIT_CODE = 130

# Per-source state-machine labels (mutually exclusive).
STATUS_BUILT = "built-needs-upload"
STATUS_REUSED = "reused-local-needs-upload"
STATUS_PUBLISHED = "already-published"
STATUS_FAILED = "failed"

ExportRecordLike = ExportRecord


class PreflightError(RuntimeError):
    """Raised when the preflight verification fails before any source is touched."""


class OrchestratorError(RuntimeError):
    """Raised for orchestrator-level failures after preflight succeeds."""


class HubVerifier(Protocol):
    """Verify that ``files`` actually exist in ``repo_id`` and return the repo SHA."""

    def __call__(self, repo_id: str, files: tuple[UploadItem, ...]) -> str: ...


class Preflight(Protocol):
    def __call__(self) -> dict[str, object]: ...


class Publisher(Protocol):
    def __call__(
        self,
        plan: UploadPlan,
        *,
        confirmation: str,
        runner: object,
        verifier: HubVerifier | None,
        timeout: float | None,
    ) -> str: ...


@dataclass
class SourceOutcome:
    source_name: str
    status: str
    included_rows: int = 0
    output_bytes: int = 0
    remote_revision: str | None = None
    note: str | None = None


@dataclass
class OrchestrationReport:
    source_count: int
    preflight: dict[str, object]
    outcomes: list[SourceOutcome] = field(default_factory=list)
    final_remote_revision: str | None = None

    def to_payload(self) -> dict[str, object]:
        return {
            "preflight": self.preflight,
            "source_count": self.source_count,
            "outcomes": [
                {
                    "source_name": outcome.source_name,
                    "status": outcome.status,
                    "included_rows": outcome.included_rows,
                    "output_bytes": outcome.output_bytes,
                    "remote_revision": outcome.remote_revision,
                    "note": outcome.note,
                }
                for outcome in self.outcomes
            ],
            "final_remote_revision": self.final_remote_revision,
        }


# ---------------------------------------------------------------------------
# Publication state
# ---------------------------------------------------------------------------


def _atomic_write_json(path: Path, payload: dict[str, object]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    body = json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n"
    temp = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
    try:
        temp.write_text(body, encoding="utf-8")
        with open(temp, "rb") as handle:
            os.fsync(handle.fileno())
        directory_fd = os.open(str(path.parent), os.O_RDONLY)
        try:
            os.fsync(directory_fd)
        finally:
            os.close(directory_fd)
        os.replace(temp, path)
    finally:
        if temp.exists():
            temp.unlink()


def read_publication_state(data_root: Path) -> dict[str, object]:
    state_path = data_root / PUBLICATION_STATE_FILENAME
    if not state_path.is_file():
        return {"schema_version": 1, "published": {}}
    return cast_dict(json.loads(state_path.read_text(encoding="utf-8")))


def _write_publication_state(
    data_root: Path,
    *,
    source_name: str,
    source_sha256: str,
    output_sha256: str,
    output_bytes: int,
    remote_revision: str,
    artifact_identity: str,
    completed_at: str,
) -> dict[str, object]:
    state = read_publication_state(data_root)
    if state.get("schema_version") != 1:
        raise OrchestratorError(
            f"unsupported publication state schema: {state.get('schema_version')!r}"
        )
    published = cast_dict(state.setdefault("published", {}))
    published[source_name] = {
        "source_sha256": source_sha256,
        "output_sha256": output_sha256,
        "output_bytes": output_bytes,
        "remote_revision": remote_revision,
        "artifact_identity": artifact_identity,
        "completed_at": completed_at,
    }
    state["last_updated_at"] = completed_at
    _atomic_write_json(data_root / PUBLICATION_STATE_FILENAME, state)
    return state


def cast_dict(value: object) -> dict[str, object]:
    if not isinstance(value, dict):
        raise OrchestratorError(f"expected dict, got {type(value).__name__}")
    return value


# ---------------------------------------------------------------------------
# Preflight
# ---------------------------------------------------------------------------


def _probe_osmium_version(executable: str) -> str:
    """Run ``<executable> --version`` and assert the output looks like osmium."""
    binary = shutil.which(executable) or executable
    try:
        completed = subprocess.run(  # noqa: S603 - controlled argument array, no shell
            [binary, "--version"],
            check=True,
            shell=False,
            capture_output=True,
            text=True,
            timeout=15,
        )
    except (FileNotFoundError, subprocess.CalledProcessError) as error:
        raise PreflightError(f"osmium --version failed for {executable}: {error}") from error
    output = completed.stdout or completed.stderr or ""
    if "libosmium" not in output and "osmium version" not in output:
        raise PreflightError(
            f"osmium at {binary!r} does not look like a real osmium-tool binary: {output!r}"
        )
    return output.splitlines()[0].strip() if output.strip() else ""


def default_preflight(
    paths: Paths,
    *,
    confirm_repo: str,
    osmium_executable: str,
    hf_executable: str,
    data_template_root: Path | None = None,
) -> dict[str, object]:
    try:
        paths.validate()
    except Exception as error:
        raise PreflightError(f"path validation failed: {error}") from error
    if confirm_repo != REPO_ID:
        raise PreflightError(f"--confirm-repo must equal {REPO_ID!r} (got {confirm_repo!r})")
    if shutil.which(osmium_executable) is None:
        raise PreflightError(f"osmium executable not found: {osmium_executable}")
    if shutil.which(hf_executable) is None:
        raise PreflightError(f"hf executable not found: {hf_executable}")

    osmium_version_output = _probe_osmium_version(osmium_executable)

    try:
        completed = subprocess.run(  # noqa: S603 - controlled argument array, no shell
            [hf_executable, "auth", "whoami"],
            check=True,
            shell=False,
            capture_output=True,
            text=True,
            timeout=15,
        )
    except (subprocess.CalledProcessError, subprocess.TimeoutExpired) as error:
        raise PreflightError(f"hf authentication check failed: {error}") from error

    whoami_lines = completed.stdout.splitlines()
    whoami = whoami_lines[0].strip() if whoami_lines else ""

    if not os.access(paths.source_root, os.R_OK):
        raise PreflightError(f"source root is not readable: {paths.source_root}")
    if not os.access(paths.data_root, os.W_OK):
        raise PreflightError(f"data root is not writable: {paths.data_root}")

    sources = discover_sources(paths.source_root)
    from osm_polygon_description_tag.manifest import (
        TRANSFORM_ALGORITHM_VERSION,
        current_area_policy_sha256,
    )

    return {
        "osmium_executable": osmium_executable,
        "osmium_version": osmium_version_output,
        "hf_executable": hf_executable,
        "hf_whoami": whoami,
        "source_root": str(paths.source_root),
        "data_root": str(paths.data_root),
        "export_config": str(osmium_export_config()),
        "card_template": str(dataset_card_template()),
        "repo_id": REPO_ID,
        "confirm_repo": confirm_repo,
        "source_count": len(sources),
        "transform_algorithm_version": TRANSFORM_ALGORITHM_VERSION,
        "area_policy_sha256": current_area_policy_sha256(),
    }


# ---------------------------------------------------------------------------
# Per-source processing
# ---------------------------------------------------------------------------


def _local_artifact_is_complete(paths: Paths, source: Source) -> tuple[bool, Manifest | None]:
    """Return (True, manifest) only when the local artifact is fully resumable."""
    output_path = paths.data_root / "data" / source.output_name
    manifest_path = (
        paths.data_root
        / "manifests"
        / f"{source.output_name.removesuffix('.parquet')}.manifest.json"
    )
    if not output_path.is_file() or not manifest_path.is_file():
        return False, None
    try:
        validate_geoparquet(output_path)
        manifest = read_manifest(manifest_path)
    except (StorageError, Exception):
        return False, None
    if not is_resumable(
        manifest,
        source_identity_for(source.path),
        output_identity_for(output_path),
    ):
        return False, None
    return True, manifest


def _process_one(
    source: Source,
    paths: Paths,
    *,
    clock: Callable[[], str],
    exporter: Callable[..., Iterable[ExportRecordLike]] | None = None,
) -> SourceOutcome:
    """Build or reuse one PBF and return the resulting per-source state.

    Returns one of three mutually exclusive states:

    - ``already-published`` — local artifact matches publication state.
    - ``reused-local-needs-upload`` — local artifact is valid but unpublished.
    - ``built-needs-upload`` — a fresh parquet was produced and must be uploaded.
    """
    complete, manifest = _local_artifact_is_complete(paths, source)
    state = read_publication_state(paths.data_root)
    published = cast_dict(state.get("published", {}))
    existing = cast_dict(published.get(source.name, {}))
    output_path = paths.data_root / "data" / source.output_name

    if (
        complete
        and manifest is not None
        and _published_state_matches(existing, manifest, source, output_path)
    ):
        return SourceOutcome(
            source_name=source.name,
            status=STATUS_PUBLISHED,
            included_rows=manifest.counts.included_rows,
            output_bytes=output_path.stat().st_size,
            remote_revision=None,
            note="already published; nothing to do",
        )

    if complete and manifest is not None:
        # Valid local artifact exists but publication state does not match.
        # Still regenerate README.md + stats.json so the upload plan is current.
        generate_dataset_docs(paths.data_root, dataset_card_template(), clock=clock)
        return SourceOutcome(
            source_name=source.name,
            status=STATUS_REUSED,
            included_rows=manifest.counts.included_rows,
            output_bytes=output_path.stat().st_size,
            remote_revision=None,
            note="local artifact reused; upload required",
        )

    result = build_one(
        source,
        paths,
        export_config=osmium_export_config(),
        executable="osmium",
        exporter=exporter,
    )
    generate_dataset_docs(paths.data_root, dataset_card_template(), clock=clock)
    return SourceOutcome(
        source_name=source.name,
        status=STATUS_BUILT,
        included_rows=result.included_rows,
        output_bytes=result.output_path.stat().st_size,
        remote_revision=None,
        note="freshly built; upload required",
    )


def _published_state_matches(
    existing: dict[str, object],
    manifest: Manifest,
    source: Source,
    output_path: Path,
) -> bool:
    source_identity = source_identity_for(source.path)
    output_identity = output_identity_for(output_path)
    if existing.get("source_sha256") != source_identity.sha256:
        return False
    if existing.get("output_sha256") != output_identity.sha256:
        return False
    if manifest.source != source_identity:
        return False
    return manifest.output == output_identity


# ---------------------------------------------------------------------------
# Publication execution (single public path)
# ---------------------------------------------------------------------------


def _default_hub_verifier(repo_id: str, files: tuple[object, ...]) -> str:
    """Default Hub verifier that performs no network and returns ``""`` for tests.

    Production code must inject a real verifier (see :class:`HubVerifier`).
    """
    return ""


def _execute_publication(
    paths: Paths,
    source: Source,
    *,
    identity_for_output: Callable[[Path], dict[str, object]],
    verifier: HubVerifier | None,
    timeout: float | None,
    upload_runner: Callable[[list[str]], str] | None,
) -> str:
    """Build the per-PBF plan, confirm its identity, and execute the upload.

    Returns the verified remote revision. Raises :class:`OrchestratorError`
    when the upload fails, when no verifier confirms the resulting state, or
    when the verifier returns an unverifiable (empty) revision.
    """
    plan = create_upload_plan(paths.data_root)
    try:
        if upload_runner is None:
            execute_upload(
                plan,
                confirmation=plan.identity_sha256,
                runner=None,
            )
        else:
            command = _per_pbf_command(paths.data_root, source.name)
            revision = upload_runner(command)
            if not revision:
                raise PublicationError("upload runner returned empty revision")
    except (PublicationError, subprocess.CalledProcessError) as error:
        raise OrchestratorError(f"upload failed for {source.name}: {error}") from error

    if verifier is None:
        raise OrchestratorError("no Hub verifier supplied; cannot record remote revision")
    try:
        verified = verifier(REPO_ID, plan.files)
    except Exception as error:
        raise OrchestratorError(f"Hub verifier failed for {source.name}: {error}") from error
    if not verified:
        raise OrchestratorError(
            f"Hub verifier returned no revision for {source.name}; refusing to record 'unknown'"
        )
    return verified


def _per_pbf_command(data_root: Path, source_name: str) -> list[str]:
    return [
        "hf",
        "upload-large-folder",
        REPO_ID,
        str(data_root),
        "--repo-type",
        "dataset",
        "--include",
        f"data/{source_name.removesuffix('.osm.pbf')}.parquet",
        "--include",
        f"manifests/{source_name.removesuffix('.osm.pbf')}.manifest.json",
        "--include",
        "README.md",
        "--include",
        "stats.json",
    ]


def _metadata_only_command(data_root: Path) -> list[str]:
    return [
        "hf",
        "upload-large-folder",
        REPO_ID,
        str(data_root),
        "--repo-type",
        "dataset",
        "--include",
        "README.md",
        "--include",
        "stats.json",
    ]


def metadata_plan_identity_payload(items: tuple[UploadItem, ...]) -> bytes:
    """Return the canonical JSON payload used to compute the metadata plan identity."""
    payload = {
        "data_root": "metadata-only",
        "files": [
            {
                "relative_path": item.relative_path,
                "sha256": item.sha256,
                "size_bytes": item.size_bytes,
            }
            for item in items
        ],
        "repo_id": REPO_ID,
    }
    body = json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n"
    return body.encode("utf-8")


# ---------------------------------------------------------------------------
# Public entry point
# ---------------------------------------------------------------------------


def run_and_publish(
    *,
    source_root: Path | None = None,
    data_root: Path | None = None,
    confirm_repo: str,
    preflight: Preflight | None = None,
    upload_runner: Callable[[list[str]], str] | None = None,
    clock: Callable[[], str] | None = None,
    paths: Paths | None = None,
    exporter: Callable[..., Iterable[ExportRecordLike]] | None = None,
    verifier: HubVerifier | None = None,
    upload_timeout: float | None = None,
) -> OrchestrationReport:
    """Stoppable, resumable build + publish for every discovered PBF."""
    if clock is None:
        clock = _default_clock
    if paths is None:
        if source_root is None or data_root is None:
            raise OrchestratorError("paths or (source_root, data_root) is required")
        paths = Paths(source_root=source_root, data_root=data_root)

    if preflight is None:
        preflight_report = default_preflight(
            paths,
            confirm_repo=confirm_repo,
            osmium_executable="osmium",
            hf_executable="hf",
        )
    else:
        preflight_report = preflight()

    sources = discover_sources(paths.source_root)
    report = OrchestrationReport(source_count=len(sources), preflight=preflight_report)

    per_pbf_upload_count = 0
    for source in sources:
        outcome = _process_one(
            source,
            paths,
            clock=clock,
            exporter=exporter,
        )
        if outcome.status == STATUS_PUBLISHED:
            report.outcomes.append(outcome)
            continue
        if outcome.status in {STATUS_BUILT, STATUS_REUSED}:
            output_path = paths.data_root / "data" / source.output_name
            try:
                revision = _execute_publication(
                    paths,
                    source,
                    identity_for_output=lambda p: {
                        "size_bytes": p.stat().st_size,
                        "sha256": file_sha256(p),
                    },
                    verifier=verifier,
                    timeout=upload_timeout,
                    upload_runner=upload_runner,
                )
            except OrchestratorError as error:
                outcome.status = STATUS_FAILED
                outcome.note = str(error)
                report.outcomes.append(outcome)
                raise
            output_path = paths.data_root / "data" / source.output_name
            plan = create_upload_plan(paths.data_root)
            _write_publication_state(
                paths.data_root,
                source_name=source.name,
                source_sha256=source_identity_for(source.path).sha256,
                output_sha256=file_sha256(output_path),
                output_bytes=output_path.stat().st_size,
                remote_revision=revision,
                artifact_identity=plan.identity_sha256,
                completed_at=clock(),
            )
            outcome.remote_revision = revision
            outcome.note = "published after verified upload"
            per_pbf_upload_count += 1
        report.outcomes.append(outcome)

    # Final completeness check uses the full resumability contract.
    _verify_final_completeness(paths, sources)

    # Explicit metadata-only final upload; failure is reported and non-zero.
    # Skip when no per-PBF upload occurred in this invocation: a no-op restart
    # has nothing new to send, so the metadata already in the repo is current.
    if per_pbf_upload_count == 0:
        return report
    final_metadata_revision = _upload_final_metadata(
        paths, verifier=verifier, upload_runner=upload_runner
    )
    if final_metadata_revision is not None:
        report.final_remote_revision = final_metadata_revision

    return report


def _verify_final_completeness(paths: Paths, sources: Iterable[Source]) -> None:
    """Every discovered source must have a complete, resumable local artifact.

    Strictly one-to-one with discovered sources: any parquet under
    ``data/`` without a matching source is reported as extra. Any source
    without a complete, resumable local artifact is reported as missing.
    A parquet without its matching manifest is also reported as missing.
    """
    discovered = {source.name: source for source in sources}
    completed: set[str] = set()
    extra_artifacts: list[str] = []
    missing_artifacts: list[str] = []
    for parquet in sorted((paths.data_root / "data").glob("*.parquet")):
        stem = parquet.name.removesuffix(".parquet")
        source_name = f"{stem}.osm.pbf"
        if source_name not in discovered:
            extra_artifacts.append(stem)
            continue
        manifest_path = paths.data_root / "manifests" / f"{stem}.manifest.json"
        if not manifest_path.is_file():
            missing_artifacts.append(f"{stem}.manifest.json")
            continue
        try:
            validate_geoparquet(parquet)
            manifest = read_manifest(manifest_path)
        except (StorageError, Exception):
            missing_artifacts.append(f"{stem} (invalid manifest or parquet)")
            continue
        source = discovered[source_name]
        if is_resumable(
            manifest,
            source_identity_for(source.path),
            output_identity_for(parquet),
        ):
            completed.add(source_name)
        else:
            missing_artifacts.append(f"{stem} (not resumable)")
    missing = set(discovered) - completed
    if missing or extra_artifacts or missing_artifacts:
        raise OrchestratorError(
            "final completeness failed: "
            f"missing={sorted(missing)} extra={sorted(extra_artifacts)} "
            f"incomplete={sorted(missing_artifacts)}"
        )


def _upload_final_metadata(
    paths: Paths,
    *,
    verifier: HubVerifier | None,
    upload_runner: Callable[[list[str]], str] | None,
) -> str | None:
    """Upload README.md + stats.json once and verify the result via Hub API.

    The upload always runs whenever at least one parquet has been written
    during this invocation. The output identity is hashed before upload so
    a no-op restart that has not written any bytes skips the upload.

    Raises :class:`OrchestratorError` when the upload fails. Returns the
    verified Hub revision, or ``None`` when there is nothing to upload.
    """
    data_dir = paths.data_root / "data"
    if not data_dir.is_dir() or not list(data_dir.glob("*.parquet")):
        return None
    metadata_files = (
        paths.data_root / "README.md",
        paths.data_root / "stats.json",
    )
    if not all(path.is_file() for path in metadata_files):
        return None
    items = tuple(
        UploadItem(
            relative_path=path.name,
            size_bytes=path.stat().st_size,
            sha256=file_sha256(path),
        )
        for path in metadata_files
    )
    metadata_plan = UploadPlan(
        repo_id=REPO_ID,
        data_root=str(paths.data_root.resolve(strict=False)),
        files=items,
        identity_sha256=publication_file_sha256_bytes(metadata_plan_identity_payload(items)),
    )
    try:
        if upload_runner is None:
            execute_upload(metadata_plan, confirmation=metadata_plan.identity_sha256)
        else:
            command = _metadata_only_command(paths.data_root)
            revision = upload_runner(command)
            if not revision:
                raise PublicationError("upload runner returned empty revision")
    except (PublicationError, subprocess.CalledProcessError) as error:
        raise OrchestratorError(f"final metadata upload failed: {error}") from error
    if verifier is None:
        raise OrchestratorError("no Hub verifier supplied; cannot record final revision")
    try:
        verified = verifier(REPO_ID, metadata_plan.files)
    except Exception as error:
        raise OrchestratorError(f"final Hub verifier failed: {error}") from error
    if not verified:
        raise OrchestratorError("Hub verifier returned no revision for final metadata")
    return verified


def _default_clock() -> str:
    from datetime import UTC, datetime

    return datetime.now(UTC).isoformat()


__all__ = [
    "INTERRUPT_EXIT_CODE",
    "PUBLICATION_STATE_FILENAME",
    "STATUS_BUILT",
    "STATUS_FAILED",
    "STATUS_PUBLISHED",
    "STATUS_REUSED",
    "OrchestrationReport",
    "OrchestratorError",
    "PreflightError",
    "SourceOutcome",
    "default_preflight",
    "read_publication_state",
    "run_and_publish",
]
