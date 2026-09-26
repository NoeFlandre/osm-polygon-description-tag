"""Guarded, resumable publication of the additive ``language-v1`` namespace.

Publication is gated three times: the caller must repeat the repository
identifier when the plan is built, must pass an explicit apply gate here, and
must supply a baseline revision so a repository that moved underneath the plan
is refused rather than overwritten.

An upload whose success cannot be established is reported as
:data:`PublishStatus.AMBIGUOUS` and its state is recorded, so the next
invocation verifies what is actually on the Hub instead of blindly re-uploading.
Nothing in this module deletes remote files or reconciles remote namespaces.
"""

import json
from collections.abc import Sequence
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path
from typing import Final, Protocol

from osm_polygon_description_tag.dataset.languages.atomic import atomic_write_json
from osm_polygon_description_tag.dataset.languages.checkpoint import exclusive_worker_lock
from osm_polygon_description_tag.dataset.languages.payloads import require_object
from osm_polygon_description_tag.publication.language import (
    LANGUAGE_CONFIG_NAME,
    LANGUAGE_REMOTE_PREFIX,
    LanguagePublicationError,
    build_language_upload_plan,
    read_language_export,
)
from osm_polygon_description_tag.publication.models import UploadItem, UploadPlan
from osm_polygon_description_tag.runtime.logging import RunLogger

PUBLICATION_STATE_FILENAME: Final = "language-publication.json"
STATE_SCHEMA_VERSION: Final = 1


class PublishStatus(StrEnum):
    """What is actually known about one publication attempt."""

    PLANNED = "planned"
    VERIFIED = "verified"
    AMBIGUOUS = "ambiguous"
    DRIFTED = "drifted"
    UNVERIFIED = "unverified"


@dataclass(frozen=True, slots=True)
class RemoteFile:
    """One file's identity as the Hub reports it."""

    relative_path: str
    size_bytes: int
    sha256: str


class LanguageHub(Protocol):
    """The narrow Hub surface this module needs.

    Implemented against ``huggingface_hub`` in production and faked in tests;
    no test ever reaches the network.
    """

    def repo_revision(self, repo_id: str) -> str:
        """Return the dataset repository's current commit SHA."""

    def upload(self, plan: UploadPlan, *, parent_revision: str | None = None) -> None:
        """Upload exactly the plan's files."""

    def paths_info(
        self, repo_id: str, revision: str, paths: Sequence[str]
    ) -> tuple[RemoteFile, ...]:
        """Return remote identities for ``paths`` at ``revision``."""

    def dataset_configs(self, repo_id: str, revision: str) -> tuple[str, ...]:
        """Return the configuration names the Dataset Viewer exposes."""


@dataclass(frozen=True, slots=True)
class PublicationOutcome:
    """The verified result of one publication attempt."""

    status: PublishStatus
    repo_id: str
    plan_identity: str
    revision: str | None
    verified_files: tuple[str, ...]
    issues: tuple[str, ...]

    @property
    def is_verified(self) -> bool:
        """Return whether every planned file was confirmed on the Hub."""
        return self.status is PublishStatus.VERIFIED

    def to_payload(self) -> dict[str, object]:
        return {
            "state_schema_version": STATE_SCHEMA_VERSION,
            "status": str(self.status),
            "repo_id": self.repo_id,
            "plan_identity": self.plan_identity,
            "revision": self.revision,
            "verified_files": list(self.verified_files),
            "issues": list(self.issues),
        }

    @classmethod
    def from_payload(cls, payload: object) -> "PublicationOutcome":
        """Rebuild a recorded outcome, rejecting any malformed field."""
        reader = require_object(payload, error=LanguagePublicationError, label="publication state")
        if reader.integer("state_schema_version") != STATE_SCHEMA_VERSION:
            raise LanguagePublicationError("unsupported publication state schema version")
        revision = reader.raw("revision")
        if revision is not None and not isinstance(revision, str):
            raise LanguagePublicationError("publication state revision must be a string or null")
        return cls(
            status=_status(reader.text("status")),
            repo_id=reader.text("repo_id"),
            plan_identity=reader.text("plan_identity"),
            revision=revision,
            verified_files=reader.texts("verified_files"),
            issues=reader.texts("issues"),
        )


def _status(value: str) -> PublishStatus:
    try:
        return PublishStatus(value)
    except ValueError as error:
        raise LanguagePublicationError(f"unsupported publication status: {value!r}") from error


def read_language_publication_state(state_path: Path) -> PublicationOutcome | None:
    """Read a recorded publication outcome, or ``None`` when absent."""
    if not state_path.is_file():
        return None
    try:
        text = state_path.read_text(encoding="utf-8")  # pragma: no mutate - codec alias only
        payload = json.loads(text)
    except (OSError, UnicodeError, json.JSONDecodeError) as error:
        raise LanguagePublicationError(
            f"cannot read publication state {state_path}: {error}"
        ) from error
    return PublicationOutcome.from_payload(payload)


def _file_issue(item: UploadItem, remote: RemoteFile | None) -> str | None:
    if remote is None:
        return f"remote file is missing: {item.relative_path}"
    if remote.size_bytes != item.size_bytes:
        return f"remote size differs for {item.relative_path}"
    if remote.sha256 != item.sha256:
        return f"remote checksum differs for {item.relative_path}"
    return None


def _compare_files(
    planned: Sequence[UploadItem], remote: Sequence[RemoteFile]
) -> tuple[list[str], list[str]]:
    by_path = {item.relative_path: item for item in remote}
    verified: list[str] = []
    issues: list[str] = []
    for item in planned:
        issue = _file_issue(item, by_path.get(item.relative_path))
        if issue is None:
            verified.append(item.relative_path)
        else:
            issues.append(issue)
    return verified, issues


def verify_language_publication(
    plan: UploadPlan,
    hub: LanguageHub,
    *,
    revision: str,
) -> tuple[tuple[str, ...], tuple[str, ...]]:
    """Verify every planned file and the configuration at one exact revision."""
    paths = [item.relative_path for item in plan.files]
    verified, issues = _compare_files(plan.files, hub.paths_info(plan.repo_id, revision, paths))
    if LANGUAGE_CONFIG_NAME not in hub.dataset_configs(plan.repo_id, revision):
        issues.append(
            f"the Dataset Viewer does not expose the {LANGUAGE_CONFIG_NAME} configuration"
        )
    return tuple(sorted(verified)), tuple(issues)


def _resumed_outcome(state_path: Path, plan: UploadPlan) -> PublicationOutcome | None:
    """Return a recorded outcome for exactly this plan, if one exists."""
    recorded = read_language_publication_state(state_path)
    if recorded is None or recorded.status in {PublishStatus.PLANNED, PublishStatus.DRIFTED}:
        return None
    if (recorded.plan_identity, recorded.repo_id) != (plan.identity_sha256, plan.repo_id):
        if recorded.status in {PublishStatus.AMBIGUOUS, PublishStatus.UNVERIFIED}:
            raise LanguagePublicationError("an unresolved publication belongs to another plan")
        return None
    return recorded


def _record(state_path: Path, outcome: PublicationOutcome) -> PublicationOutcome:
    atomic_write_json(state_path, outcome.to_payload())
    return outcome


def publish_language_export(
    plan: UploadPlan,
    hub: LanguageHub,
    *,
    baseline_revision: str,
    apply: bool = False,
    state_path: Path | None = None,
    logger: RunLogger | None = None,
) -> PublicationOutcome:
    """Publish one additive export behind an explicit gate, then verify it.

    Re-invoking after an ambiguous upload verifies the Hub rather than sending
    the files again; a repository whose revision moved away from
    ``baseline_revision`` is refused so a concurrent change is never clobbered.
    A failed upload is logged through ``logger`` when one is supplied.
    """
    if state_path is None:
        state_path = Path(plan.data_root) / PUBLICATION_STATE_FILENAME
    if apply:
        with exclusive_worker_lock(Path(plan.data_root) / LANGUAGE_REMOTE_PREFIX):
            _require_current_plan(plan)
            return _publish_language_export(plan, hub, baseline_revision, apply, state_path, logger)
    # ``apply`` is falsy on this path and ``_publish_language_export`` only ever tests it
    # for truth, so passing ``None`` here is equivalent. The identical call above keeps
    # the other three arguments' ``None`` variants under test.
    # pragma: no mutate start
    return _publish_language_export(plan, hub, baseline_revision, apply, state_path, logger)
    # pragma: no mutate end


def _require_current_plan(plan: UploadPlan) -> None:
    export = read_language_export(Path(plan.data_root))
    current = build_language_upload_plan(export, plan.repo_id, confirm_repo=plan.repo_id)
    if current != plan:
        raise LanguagePublicationError("export changed after the upload plan was created")


def _publish_language_export(
    plan: UploadPlan,
    hub: LanguageHub,
    baseline_revision: str,
    apply: bool,
    state_path: Path,
    logger: RunLogger | None,
) -> PublicationOutcome:
    resumed = _resumed_outcome(state_path, plan)
    current = hub.repo_revision(plan.repo_id)
    if resumed is None and current != baseline_revision:
        outcome = _drifted(plan, baseline_revision, current)
        return _record(state_path, outcome) if apply else outcome
    if not apply:
        return PublicationOutcome(
            PublishStatus.PLANNED, plan.repo_id, plan.identity_sha256, current, (), ()
        )
    return _apply_publication(plan, hub, state_path, resumed, current, logger)


def _apply_publication(
    plan: UploadPlan,
    hub: LanguageHub,
    state_path: Path,
    resumed: PublicationOutcome | None,
    current: str,
    logger: RunLogger | None,
) -> PublicationOutcome:
    if resumed is None:
        _ambiguous(state_path, plan, current)
        failure = _upload(plan, hub, current)
        if failure is not None:
            _log_upload_failure(logger, plan, failure)
            return _ambiguous(state_path, plan, current, failure)
    return _verified_outcome(plan, hub, state_path)


def _drifted(plan: UploadPlan, baseline: str, current: str) -> PublicationOutcome:
    return PublicationOutcome(
        PublishStatus.DRIFTED,
        plan.repo_id,
        plan.identity_sha256,
        current,
        (),
        (
            f"repository moved from {baseline} to {current}; "
            "re-plan against the current revision before publishing",
        ),
    )


def _upload(plan: UploadPlan, hub: LanguageHub, parent_revision: str) -> str | None:
    """Attempt the upload, returning the failure's text or ``None`` on success.

    Any failure is an unknown outcome: the commit may have landed before the
    error surfaced, so the next invocation verifies the Hub. The catch stays
    broad on purpose (tests pin that a generic error is treated as ambiguous);
    ``BaseException`` subclasses such as ``KeyboardInterrupt`` still propagate.
    """
    try:
        hub.upload(plan, parent_revision=parent_revision)
    except Exception as error:
        return f"{type(error).__name__}: {error}"
    return None


def _log_upload_failure(logger: RunLogger | None, plan: UploadPlan, failure: str) -> None:
    if logger is not None:
        logger.event(
            "language_publication_upload_failed",
            level="WARNING",
            result="ambiguous",
            reason=failure,
            identity_sha256=plan.identity_sha256,
        )


def _ambiguous(
    state_path: Path, plan: UploadPlan, revision: str, failure: str | None = None
) -> PublicationOutcome:
    cause = () if failure is None else (f"upload error: {failure}",)
    return _record(
        state_path,
        PublicationOutcome(
            PublishStatus.AMBIGUOUS,
            plan.repo_id,
            plan.identity_sha256,
            revision,
            (),
            (
                "the upload did not report success; verify the repository "
                "before attempting to publish again",
                *cause,
            ),
        ),
    )


def _verified_outcome(plan: UploadPlan, hub: LanguageHub, state_path: Path) -> PublicationOutcome:
    revision = hub.repo_revision(plan.repo_id)
    verified, issues = verify_language_publication(plan, hub, revision=revision)
    status = PublishStatus.VERIFIED if not issues else PublishStatus.UNVERIFIED
    return _record(
        state_path,
        PublicationOutcome(status, plan.repo_id, plan.identity_sha256, revision, verified, issues),
    )


__all__ = [
    "PUBLICATION_STATE_FILENAME",
    "STATE_SCHEMA_VERSION",
    "LanguageHub",
    "PublicationOutcome",
    "PublishStatus",
    "RemoteFile",
    "publish_language_export",
    "read_language_publication_state",
    "verify_language_publication",
]
