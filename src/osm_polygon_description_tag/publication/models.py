from __future__ import annotations

import json
from collections.abc import Callable
from dataclasses import dataclass

REPO_ID = "NoeFlandre/osm-polygon-description-tag"
RETRYABLE_EXIT_CODES: frozenset[int] = frozenset({5, 429, 502, 503, 504})
DEFAULT_MAX_RETRIES = 3
DEFAULT_BACKOFF_SECONDS = 1.0
DEFAULT_BACKOFF_FACTOR = 2.0
DEFAULT_BACKOFF_CAP_SECONDS = 30.0

Runner = Callable[[list[str]], None]


class PublicationError(ValueError):
    """Raised for publication planning or execution failures."""


class PublishRetry(PublicationError):
    """Raised internally to signal a retryable upload failure."""

    def __init__(self, message: str, *, exit_code: int | None, kind: str) -> None:
        super().__init__(message)
        self.exit_code = exit_code
        self.kind = kind


@dataclass(frozen=True)
class UploadItem:
    relative_path: str
    size_bytes: int
    sha256: str


@dataclass(frozen=True)
class UploadPlan:
    repo_id: str
    data_root: str
    files: tuple[UploadItem, ...]
    identity_sha256: str

    def to_payload(self) -> dict[str, object]:
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
            "repo_id": self.repo_id,
        }

    def to_json(self) -> str:
        return json.dumps(self.to_payload(), ensure_ascii=False, indent=2, sort_keys=True) + "\n"
