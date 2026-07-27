"""Stoppable, resumable per-PBF build + publish orchestrator.

The single public command (``run_and_publish``) performs a preflight,
iterates discovered sources in deterministic filename order, and for each one:

1. Reuses the existing output if its manifest agrees with the current
   source/output identity, schema versions, area-policy checksum, transform
   algorithm version, and code revision.
2. Otherwise rebuilds exactly that PBF with the real osmium binary.
3. Validates the produced GeoParquet and manifest, regenerates
   ``stats.json`` and ``README.md``, and computes a per-PBF upload plan
   containing exactly four files (Parquet, manifest, stats, README).
4. Uploads that four-file plan and records the returned remote commit
   identity in ``publication-state.json`` atomically.

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
from typing import Protocol, cast

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
    output_identity_for,
    read_manifest,
    source_identity_for,
)
from osm_polygon_description_tag.pipeline import build_one
from osm_polygon_description_tag.publication import (
    REPO_ID,
    UploadPlan,
)
from osm_polygon_description_tag.reporting import generate_dataset_docs
from osm_polygon_description_tag.storage import StorageError, validate_geoparquet

PUBLICATION_STATE_FILENAME = "publication-state.json"
INTERRUPT_EXIT_CODE = 130
_INTERRUPT_SENTINEL = object()
ExportRecordLike = ExportRecord


class PreflightError(RuntimeError):
    """Raised when the preflight verification fails before any source is touched."""


class OrchestratorError(RuntimeError):
    """Raised for orchestrator-level failures after preflight succeeds."""


class Publisher(Protocol):
    def __call__(
        self,
        plan: UploadPlan,
        *,
        confirmation: str,
        runner: object,
    ) -> str:
        """Execute the upload and return the remote commit/revision identifier."""


class Preflight(Protocol):
    def __call__(self) -> dict[str, object]:
        """Return a structured preflight report.

        Implementations must raise :class:`PreflightError` for any failure
        (missing tool, failed authentication, unwritable data root, etc.).
        """


@dataclass
class SourceOutcome:
    source_name: str
    status: str  # "built", "skipped", "rebuilt", "uploaded", "published", "failed"
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
    """Atomically write JSON with trailing newline and directory fsync."""
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


def default_preflight(
    paths: Paths,
    *,
    confirm_repo: str,
    osmium_executable: str,
    hf_executable: str,
    data_template_root: Path | None = None,
) -> dict[str, object]:
    """Verify tools, paths, and Hugging Face authentication before processing.

    Raises :class:`PreflightError` for any failure. The default runner calls
    ``hf auth whoami`` to confirm authentication; tests must inject a fake
    preflight callable.
    """
    paths.validate()

    if shutil.which(osmium_executable) is None:
        raise PreflightError(f"osmium executable not found: {osmium_executable}")
    if shutil.which(hf_executable) is None:
        raise PreflightError(f"hf executable not found: {hf_executable}")

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

    if confirm_repo != REPO_ID:
        raise PreflightError(f"--confirm-repo must equal {REPO_ID!r} (got {confirm_repo!r})")

    if not os.access(paths.source_root, os.R_OK):
        raise PreflightError(f"source root is not readable: {paths.source_root}")
    if not os.access(paths.data_root, os.W_OK):
        raise PreflightError(f"data root is not writable: {paths.data_root}")
    if (paths.data_root / "data").exists() and not os.access(paths.data_root / "data", os.W_OK):
        raise PreflightError(f"data directory is not writable: {paths.data_root / 'data'}")

    sources = discover_sources(paths.source_root)
    return {
        "osmium_executable": osmium_executable,
        "hf_executable": hf_executable,
        "hf_whoami": whoami,
        "source_root": str(paths.source_root),
        "data_root": str(paths.data_root),
        "data_template_root": str(data_template_root) if data_template_root else None,
        "export_config": str(osmium_export_config()),
        "card_template": str(dataset_card_template()),
        "repo_id": REPO_ID,
        "confirm_repo": confirm_repo,
        "source_count": len(sources),
    }


# ---------------------------------------------------------------------------
# Upload command construction
# ---------------------------------------------------------------------------


def _per_pbf_upload_command(data_root: Path, source_name: str) -> list[str]:
    """Return the exact per-PBF ``hf upload-large-folder`` command.

    Contains only the four artifacts belonging to this source's slice plus
    ``stats.json`` and ``README.md`` regenerated by ``generate_dataset_docs``.
    """
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


def _per_pbf_upload_plan(data_root: Path, source_name: str) -> UploadPlan:
    """Construct an UploadPlan containing exactly four files for ``source_name``."""
    from osm_polygon_description_tag.publication import UploadItem, file_sha256_bytes

    stem = source_name.removesuffix(".osm.pbf")
    items: list[UploadItem] = []
    for relative in (
        "README.md",
        "stats.json",
        f"data/{stem}.parquet",
        f"manifests/{stem}.manifest.json",
    ):
        path = data_root / relative
        items.append(
            UploadItem(
                relative_path=relative,
                size_bytes=path.stat().st_size,
                sha256=file_sha256(path),
            )
        )
    items.sort(key=lambda item: item.relative_path)
    payload: dict[str, object] = {
        "data_root": str(data_root.resolve(strict=False)),
        "files": [
            {"relative_path": i.relative_path, "sha256": i.sha256, "size_bytes": i.size_bytes}
            for i in items
        ],
        "repo_id": REPO_ID,
    }
    return UploadPlan(
        repo_id=REPO_ID,
        data_root=str(data_root.resolve(strict=False)),
        files=tuple(items),
        identity_sha256=file_sha256_bytes(
            (json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n").encode(
                "utf-8"
            )
        ),
    )


def _default_upload_runner(command: list[str]) -> str:
    """Execute ``command`` and return a remote commit/revision.

    The default uses ``subprocess.run(..., capture_output=True, text=True)``
    and uses the first line of stdout as the returned revision.
    """
    completed = subprocess.run(  # noqa: S603 - controlled argument array, no shell
        command,
        check=True,
        shell=False,
        capture_output=True,
        text=True,
        timeout=300,
    )
    first_line = completed.stdout.splitlines()[0].strip() if completed.stdout else ""
    return first_line or "unknown"


# ---------------------------------------------------------------------------
# Per-source processing
# ---------------------------------------------------------------------------


def _process_one(
    source: Source,
    paths: Paths,
    *,
    upload_runner: Callable[[list[str]], str],
    identity_for_output: Callable[[Path], dict[str, object]],
    clock: Callable[[], str],
    exporter: Callable[..., Iterable[ExportRecordLike]] | None = None,
) -> SourceOutcome:
    """Build (or reuse) one PBF and record the result without uploading.

    Returns a :class:`SourceOutcome`. The caller uploads + records state.
    """
    output_path = paths.data_root / "data" / source.output_name
    manifest_path = (
        paths.data_root
        / "manifests"
        / f"{source.output_name.removesuffix('.parquet')}.manifest.json"
    )

    state = read_publication_state(paths.data_root)
    published = cast_dict(state.get("published", {}))
    existing = cast_dict(published.get(source.name, {}))
    source_identity = source_identity_for(source.path)
    source_sha = source_identity.sha256

    if (
        existing
        and existing.get("source_sha256") == source_sha
        and output_path.is_file()
        and manifest_path.is_file()
    ):
        # Reuse only when both Parquet and manifest are still valid.
        manifest: Manifest | None
        try:
            validate_geoparquet(output_path)
            manifest = read_manifest(manifest_path)
        except (StorageError, Exception):
            manifest = None
        if manifest is not None and output_identity_for(output_path) == manifest.output:
            return SourceOutcome(
                source_name=source.name,
                status="skipped",
                included_rows=manifest.counts.included_rows,
                output_bytes=output_path.stat().st_size,
                remote_revision=cast("str | None", existing.get("remote_revision")),
                note="local artifact matches published state",
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
        status="built" if result.status == "built" else result.status,
        included_rows=result.included_rows,
        output_bytes=result.output_path.stat().st_size,
        remote_revision=None,
        note="rebuilt" if result.status == "built" else result.status,
    )


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
) -> OrchestrationReport:
    """Stoppable, resumable build + publish for every discovered PBF."""
    if clock is None:
        clock = _default_clock
    if upload_runner is None:
        upload_runner = _default_upload_runner

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

    last_revision: str | None = None
    for source in sources:
        outcome = _process_one(
            source,
            paths,
            upload_runner=upload_runner,
            identity_for_output=lambda p: {
                "size_bytes": p.stat().st_size,
                "sha256": file_sha256(p),
            },
            clock=clock,
            exporter=exporter,
        )
        if outcome.status in {"built", "skipped"}:
            plan = _per_pbf_upload_plan(paths.data_root, source.name)
            command = _per_pbf_upload_command(paths.data_root, source.name)
            try:
                revision = upload_runner(command)
            except subprocess.CalledProcessError as error:
                outcome.status = "failed"
                outcome.note = f"upload failed: {error}"
                report.outcomes.append(outcome)
                raise OrchestratorError(outcome.note) from error
            output_path = paths.data_root / "data" / source.output_name
            manifest_path = (
                paths.data_root
                / "manifests"
                / f"{source.output_name.removesuffix('.parquet')}.manifest.json"
            )
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
            outcome.status = "published"
            outcome.remote_revision = revision
            last_revision = revision
        report.outcomes.append(outcome)

    report.final_remote_revision = last_revision

    # Final completeness check.
    discovered_stems = {source.output_name.removesuffix(".parquet") for source in sources}
    completed_stems: set[str] = set()
    for parquet in sorted((paths.data_root / "data").glob("*.parquet")):
        stem = parquet.name.removesuffix(".parquet")
        manifest_path = paths.data_root / "manifests" / f"{stem}.manifest.json"
        if manifest_path.is_file():
            try:
                validate_geoparquet(parquet)
                read_manifest(manifest_path)
            except Exception:  # noqa: S112 - intentional silent failure for partial state
                continue
            completed_stems.add(stem)
    if completed_stems != discovered_stems:
        missing = discovered_stems - completed_stems
        extra = completed_stems - discovered_stems
        raise OrchestratorError(
            f"final completeness failed: missing={sorted(missing)} extra={sorted(extra)}"
        )

    # Final idempotent metadata upload (only when the dataset has artifacts).
    if completed_stems:
        try:
            final_runner: Callable[[list[str]], str] = upload_runner
            final_revision = final_runner(_per_pbf_upload_command(paths.data_root, "__final__"))
            report.final_remote_revision = final_revision
        except subprocess.CalledProcessError:
            pass

    return report


def _default_clock() -> str:
    from datetime import UTC, datetime

    return datetime.now(UTC).isoformat()


__all__ = [
    "INTERRUPT_EXIT_CODE",
    "PUBLICATION_STATE_FILENAME",
    "OrchestrationReport",
    "OrchestratorError",
    "PreflightError",
    "SourceOutcome",
    "_default_upload_runner",
    "default_preflight",
    "read_publication_state",
    "run_and_publish",
]
