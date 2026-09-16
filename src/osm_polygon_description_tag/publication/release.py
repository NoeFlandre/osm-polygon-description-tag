"""Deterministic metadata release: compute, validate, publish, verify.

The release path is intentionally narrow. It recomputes the dataset card and
``stats.json`` from the complete validated Parquet inventory, publishes only
those metadata artifacts to the exact Hub dataset, and verifies the remote
files afterwards. Source data, manifests, and unrelated Hub files are never
rewritten by this path.

Determinism: statistics come from every valid published row and its matching
manifest. Regeneration writes a file only when its bytes actually change, so a
second release run over unchanged artifacts is a no-op that produces the same
plan identity and the same remote revision.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any

from osm_polygon_description_tag.dataset.manifest import _manifest_path_for
from osm_polygon_description_tag.dataset.reporting import generate_dataset_docs
from osm_polygon_description_tag.publication.models import (
    REPO_ID,
    PublicationError,
    Runner,
    UploadItem,
    UploadPlan,
)
from osm_polygon_description_tag.publication.planning import (
    _build_metadata_only_upload_plan,
    _validate_manifest,
)
from osm_polygon_description_tag.publication.upload import execute_upload
from osm_polygon_description_tag.publication.verification import (
    HubVerifier,
    build_default_hub_verifier,
)


@dataclass(frozen=True)
class ReleaseReport:
    """Evidence for one metadata release run."""

    repo_id: str
    data_root: str
    plan_identity_sha256: str
    files: tuple[UploadItem, ...]
    validated_parquet_files: int
    rows: int
    published: bool
    revision: str | None

    def to_payload(self) -> dict[str, Any]:
        return {
            "data_root": self.data_root,
            "files": [
                {
                    "relative_path": item.relative_path,
                    "sha256": item.sha256,
                    "size_bytes": item.size_bytes,
                }
                for item in self.files
            ],
            "plan_identity_sha256": self.plan_identity_sha256,
            "published": self.published,
            "repo_id": self.repo_id,
            "revision": self.revision,
            "rows": self.rows,
            "validated_parquet_files": self.validated_parquet_files,
        }


def _require_exact_repo(confirm_repo: str) -> None:
    if confirm_repo != REPO_ID:
        raise PublicationError(
            f"repository confirmation must equal {REPO_ID!r} (got {confirm_repo!r})"
        )


def validate_published_inventory(data_root: Path) -> int:
    """Validate every published Parquet against its manifest; return the file count.

    An empty or missing ``data/`` directory is refused: a release must never
    publish statistics computed over nothing.
    """
    data_dir = data_root / "data"
    if data_dir.is_symlink() or not data_dir.is_dir():
        raise PublicationError(f"published data directory missing: {data_dir}")
    parquets = sorted(data_dir.glob("*.parquet"), key=lambda path: path.name)
    if not parquets:
        raise PublicationError(f"no published Parquet files under {data_dir}")
    for parquet in parquets:
        _validate_manifest(_manifest_path_for(parquet.name, data_root), parquet)
    return len(parquets)


def _publish(
    plan: UploadPlan,
    *,
    runner: Runner | None,
    verifier: HubVerifier | None,
) -> str:
    execute_upload(plan, confirmation=plan.identity_sha256, runner=runner)
    resolved_verifier = build_default_hub_verifier() if verifier is None else verifier
    revision = resolved_verifier(plan.repo_id, plan.files)
    if not revision:
        raise PublicationError("hub verification returned an empty revision")
    return revision


def release_metadata(
    data_root: Path,
    template_path: Path,
    *,
    confirm_repo: str,
    apply: bool,
    runner: Runner | None = None,
    verifier: HubVerifier | None = None,
) -> ReleaseReport:
    """Compute, validate, and optionally publish the card and statistics report.

    ``apply=False`` performs the full local compute-and-validate pass and
    returns the exact plan that would be uploaded, without touching the
    network.
    """
    _require_exact_repo(confirm_repo)
    resolved_root = data_root.resolve(strict=False)
    validated_files = validate_published_inventory(resolved_root)
    stats = generate_dataset_docs(resolved_root, template_path)
    plan = _build_metadata_only_upload_plan(resolved_root)
    revision = _publish(plan, runner=runner, verifier=verifier) if apply else None
    return ReleaseReport(
        repo_id=plan.repo_id,
        data_root=plan.data_root,
        plan_identity_sha256=plan.identity_sha256,
        files=plan.files,
        validated_parquet_files=validated_files,
        rows=int(stats["rows"]),
        published=apply,
        revision=revision,
    )


__all__ = ["ReleaseReport", "release_metadata", "validate_published_inventory"]
