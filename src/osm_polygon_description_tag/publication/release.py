"""Deterministic metadata release: compute, validate, publish, verify.

The release path is intentionally narrow. It recomputes the dataset card and
``stats.json`` from the complete validated Parquet inventory, pins and verifies
the remote data/manifest inventory before publishing, publishes only those
metadata artifacts to the exact Hub dataset, and verifies the remote files and
inventory afterwards. Source data, manifests, and unrelated Hub files are
never rewritten by this path.

Determinism: statistics come from every valid published row and its matching
manifest. Regeneration writes a file only when its bytes actually change, so a
second release run over unchanged artifacts is a no-op that produces the same
plan identity and the same remote revision.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any

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
    _collect_data_items,
    _collect_manifest_items,
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
    data_revision: str | None = None

    def to_payload(self) -> dict[str, Any]:
        return {
            "data_root": self.data_root,
            "data_revision": self.data_revision,
            "files": _upload_items_payload(self.files),
            "plan_identity_sha256": self.plan_identity_sha256,
            "published": self.published,
            "repo_id": self.repo_id,
            "revision": self.revision,
            "rows": self.rows,
            "validated_parquet_files": self.validated_parquet_files,
        }


def _upload_items_payload(files: tuple[UploadItem, ...]) -> list[dict[str, object]]:
    """Serialize upload evidence in the stable report order."""
    return [
        {
            "relative_path": item.relative_path,
            "sha256": item.sha256,
            "size_bytes": item.size_bytes,
        }
        for item in files
    ]


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
    return sum(item.relative_path.startswith("data/") for item in _published_inventory(data_root))


def _require_real_directory(path: Path, message: str) -> None:
    if path.is_symlink() or not path.is_dir():
        raise PublicationError(f"{message}: {path}")


def _require_nonempty_inventory(items: list[UploadItem], path: Path) -> list[UploadItem]:
    if not items:
        raise PublicationError(f"no published Parquet files under {path}")
    return items


def _published_inventory(data_root: Path) -> tuple[UploadItem, ...]:
    data_dir = data_root / "data"
    _require_real_directory(data_dir, "published data directory missing")
    data_items = _require_nonempty_inventory(_collect_data_items(data_root), data_dir)
    manifests_dir = data_root / "manifests"
    _require_real_directory(manifests_dir, "published manifest directory missing")
    manifest_items = _collect_manifest_items(data_root)
    return tuple(sorted((*data_items, *manifest_items), key=lambda item: item.relative_path))


def _require_inventory_verifier(verifier: HubVerifier) -> Any:
    inventory_verifier = getattr(verifier, "verify_inventory", None)
    if not callable(inventory_verifier):
        raise PublicationError(
            "--apply requires a verifier for the complete data/manifest inventory"
        )
    return inventory_verifier


def _publish(
    plan: UploadPlan,
    *,
    inventory: tuple[UploadItem, ...],
    runner: Runner | None,
    verifier: HubVerifier | None,
) -> tuple[str, str]:
    resolved_verifier = build_default_hub_verifier() if verifier is None else verifier
    inventory_verifier = _require_inventory_verifier(resolved_verifier)
    data_revision = inventory_verifier(plan.repo_id, inventory)
    if not data_revision:
        raise PublicationError("hub inventory verification returned an empty revision")
    execute_upload(plan, confirmation=plan.identity_sha256, runner=runner)
    revision = resolved_verifier(plan.repo_id, plan.files)
    if not revision:
        raise PublicationError("hub verification returned an empty revision")
    verified_revision = inventory_verifier(plan.repo_id, inventory, revision=revision)
    if verified_revision != revision:
        raise PublicationError(
            "hub inventory verification returned a revision different from the upload"
        )
    return data_revision, revision


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
    inventory = _published_inventory(resolved_root)
    data_revision: str | None = None
    revision: str | None = None
    if apply:
        data_revision, revision = _publish(
            plan,
            inventory=inventory,
            runner=runner,
            verifier=verifier,
        )
    return ReleaseReport(
        repo_id=plan.repo_id,
        data_root=plan.data_root,
        plan_identity_sha256=plan.identity_sha256,
        files=plan.files,
        validated_parquet_files=validated_files,
        rows=int(stats["rows"]),
        published=apply,
        revision=revision,
        data_revision=data_revision,
    )


__all__ = ["ReleaseReport", "release_metadata", "validate_published_inventory"]
