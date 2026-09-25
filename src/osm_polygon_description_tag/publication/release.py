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

import os
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from osm_polygon_description_tag.dataset.docs import generate_dataset_docs
from osm_polygon_description_tag.publication.models import (
    REPO_ID,
    PublicationError,
    Runner,
    UploadItem,
    UploadPlan,
)
from osm_polygon_description_tag.publication.planning import (
    _collect_data_items,
    _collect_manifest_items,
    build_metadata_only_upload_plan,
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


@dataclass(frozen=True)
class _RemoteReleaseContext:
    verifier: HubVerifier
    inventory: tuple[UploadItem, ...]
    data_revision: str


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
    publish statistics computed over nothing. Legacy source artifacts may
    contain rows rejected by the final text predicate; those rows remain in
    the inventory but are excluded from release statistics and media.
    """
    return sum(
        item.relative_path.startswith("data/")
        for item in _published_inventory(data_root, require_successful_text=False)
    )


def _require_real_directory(path: Path, message: str) -> None:
    if path.is_symlink() or not path.is_dir():
        raise PublicationError(f"{message}: {path}")


def _require_nonempty_inventory(items: list[UploadItem], path: Path) -> list[UploadItem]:
    if not items:
        raise PublicationError(f"no published Parquet files under {path}")
    return items


def _published_inventory(
    data_root: Path,
    *,
    require_successful_text: bool = True,
) -> tuple[UploadItem, ...]:
    data_dir = data_root / "data"
    manifests_dir = data_root / "manifests"
    _require_real_directory(data_dir, "published data directory missing")
    # Checked before the data items are collected: collecting them reads each
    # file's manifest, so a missing directory otherwise surfaced as "cannot
    # read manifest <path>: No such file" for one arbitrary shard, which says
    # nothing about the directory being absent.
    _require_real_directory(manifests_dir, "published manifest directory missing")
    data_items = _require_nonempty_inventory(
        _collect_data_items(
            data_root,
            require_successful_text=require_successful_text,
        ),
        data_dir,
    )
    manifest_items = _collect_manifest_items(data_root)
    return tuple(sorted((*data_items, *manifest_items), key=lambda item: item.relative_path))


def _require_inventory_verifier(verifier: HubVerifier) -> Any:
    inventory_verifier = getattr(verifier, "verify_inventory", None)
    if not callable(inventory_verifier):
        raise PublicationError(
            "--apply requires a verifier for the complete data/manifest inventory"
        )
    return inventory_verifier


def _verify_inventory_revision(
    inventory_verifier: Any,
    repo_id: str,
    inventory: tuple[UploadItem, ...],
    revision: str,
) -> str:
    current_data_revision = inventory_verifier(repo_id, inventory, revision=revision)
    if not current_data_revision:
        raise PublicationError("hub inventory verification returned an empty revision")
    return str(current_data_revision)


def _matching_metadata_revision(
    plan: UploadPlan,
    inventory: tuple[UploadItem, ...],
    verifier: HubVerifier,
    inventory_verifier: Any,
) -> tuple[str, str] | None:
    matching_revision = getattr(verifier, "matching_revision", None)
    if not callable(matching_revision):
        return None
    existing_revision = matching_revision(plan.repo_id, plan.files)
    if not existing_revision:
        return None
    current_data_revision = _verify_inventory_revision(
        inventory_verifier,
        plan.repo_id,
        inventory,
        str(existing_revision),
    )
    return current_data_revision, str(existing_revision)


def _verify_metadata_revision(
    plan: UploadPlan,
    inventory: tuple[UploadItem, ...],
    verifier: HubVerifier,
    inventory_verifier: Any,
) -> str:
    revision = verifier(plan.repo_id, plan.files)
    if not revision:
        raise PublicationError("hub verification returned an empty revision")
    verified_revision = inventory_verifier(plan.repo_id, inventory, revision=revision)
    if verified_revision != revision:
        raise PublicationError(
            "hub inventory verification returned a revision different from the upload"
        )
    return revision


def _publish(
    plan: UploadPlan,
    *,
    inventory: tuple[UploadItem, ...],
    runner: Runner | None,
    verifier: HubVerifier | None,
    data_revision: str,
) -> tuple[str, str]:
    resolved_verifier = build_default_hub_verifier() if verifier is None else verifier
    inventory_verifier = _require_inventory_verifier(resolved_verifier)
    matching = _matching_metadata_revision(
        plan,
        inventory,
        resolved_verifier,
        inventory_verifier,
    )
    if matching is not None:
        return matching
    execute_upload(
        plan,
        confirmation=plan.identity_sha256,
        runner=runner,
        parent_revision=data_revision,
    )
    revision = _verify_metadata_revision(
        plan,
        inventory,
        resolved_verifier,
        inventory_verifier,
    )
    return data_revision, revision


def _prepare_remote_release(
    data_root: Path,
    repo_id: str,
    verifier: HubVerifier | None,
) -> _RemoteReleaseContext:
    resolved_verifier = build_default_hub_verifier() if verifier is None else verifier
    inventory_verifier = _require_inventory_verifier(resolved_verifier)
    inventory = _published_inventory(data_root, require_successful_text=False)
    data_revision = inventory_verifier(repo_id, inventory)
    if not data_revision:
        raise PublicationError("hub inventory verification returned an empty revision")
    _sync_remote_card(data_root, resolved_verifier, repo_id, data_revision)
    return _RemoteReleaseContext(resolved_verifier, inventory, data_revision)


def _compute_release_artifacts(
    data_root: Path,
    template_path: Path,
) -> tuple[dict[str, Any], UploadPlan, tuple[UploadItem, ...]]:
    stats = generate_dataset_docs(
        data_root,
        template_path,
        preserve_existing=True,
    )
    plan = build_metadata_only_upload_plan(data_root)
    inventory = _published_inventory(data_root, require_successful_text=False)
    return stats, plan, inventory


def _publish_if_requested(
    context: _RemoteReleaseContext | None,
    plan: UploadPlan,
    inventory: tuple[UploadItem, ...],
    runner: Runner | None,
) -> tuple[str | None, str | None]:
    if context is None:
        return None, None
    data_revision, revision = _publish(
        plan,
        inventory=inventory,
        runner=runner,
        verifier=context.verifier,
        data_revision=context.data_revision,
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
    resolved_root = data_root.resolve()  # non-strict is the default: the root need not exist
    validated_files = validate_published_inventory(resolved_root)
    context = _prepare_remote_release(resolved_root, confirm_repo, verifier) if apply else None
    stats, plan, computed_inventory = _compute_release_artifacts(resolved_root, template_path)
    if context is not None and computed_inventory != context.inventory:
        raise PublicationError("local data/manifest inventory changed during stats generation")
    data_revision, revision = _publish_if_requested(context, plan, computed_inventory, runner)
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


def _read_remote_card(
    verifier: HubVerifier,
    repo_id: str,
    revision: str,
) -> str | None:
    """Read the remote card when the verifier exposes that optional capability."""
    read_file = getattr(verifier, "read_file", None)
    if not callable(read_file):
        return None
    try:
        remote_readme = read_file(repo_id, "README.md", revision=revision)
    except TypeError as error:
        raise PublicationError("Hub verifier read_file has an incompatible interface") from error
    if not isinstance(remote_readme, str):
        raise PublicationError("Hub verifier returned a non-text README")
    return remote_readme


def _sync_remote_card(
    data_root: Path,
    verifier: HubVerifier,
    repo_id: str,
    revision: str,
) -> None:
    """Use the pinned remote card as the release base when supported."""
    remote_readme = _read_remote_card(verifier, repo_id, revision)
    if remote_readme is None:
        return
    target = data_root / "README.md"
    current = target.read_text(encoding="utf-8") if target.is_file() else None  # pragma: no mutate
    if current == remote_readme:
        return
    temporary = target.with_name(f".{target.name}.{uuid.uuid4().hex}.tmp")
    try:
        temporary.write_text(remote_readme, encoding="utf-8", newline="")  # pragma: no mutate
        with temporary.open("rb") as handle:
            os.fsync(handle.fileno())
        os.replace(temporary, target)
    finally:
        if temporary.exists():
            temporary.unlink()


__all__ = ["ReleaseReport", "release_metadata", "validate_published_inventory"]
