"""The one place that resolves the ``huggingface_hub`` client.

``huggingface_hub`` is imported lazily so read-only and offline operations do
not pull in a network-authenticated dependency at import time. Every module
that talks to the Hub resolves ``HfApi`` and ``CommitOperationAdd`` through
the helpers here, at call time, so a test that overrides an attribute on
:data:`_huggingface_hub` is honoured everywhere.
"""

from __future__ import annotations

from typing import Any, Final

DATASET_REPO_TYPE: Final = "dataset"


class _HuggingFaceHub:
    """Lazy wrapper around the huggingface_hub package.

    Importing huggingface_hub at module load time would couple the project
    to a network-authenticated dependency even for read-only operations.
    The wrapper defers the import until a Hub client is actually needed.

    Tests may override attributes on this instance (for example,
    ``_huggingface_hub.HfApi = lambda ...``); those overrides take
    precedence over the lazy lookup.
    """

    def __init__(self) -> None:
        self._module: object | None = None

    def _resolve_module(self) -> object:
        if self._module is None:
            import huggingface_hub as _hub

            self._module = _hub
        return self._module

    def __getattr__(self, name: str) -> object:
        return getattr(self._resolve_module(), name)


_huggingface_hub = _HuggingFaceHub()


def new_hf_api() -> Any:
    """Instantiate ``HfApi``, resolved at call time so overrides are honoured."""
    api_class: Any = _huggingface_hub.HfApi
    return api_class()


def commit_operation_add(path_in_repo: str, content: object) -> Any:
    """Build one ``CommitOperationAdd`` for ``path_in_repo``."""
    operation_class: Any = _huggingface_hub.CommitOperationAdd
    return operation_class(path_in_repo=path_in_repo, path_or_fileobj=content)


def create_dataset_commit(
    api: Any,
    *,
    repo_id: str,
    operations: list[Any],
    commit_message: str,
    parent_commit: str,
) -> object:
    """Create one parent-guarded commit on a dataset repository."""
    return api.create_commit(
        repo_id=repo_id,
        operations=operations,
        repo_type=DATASET_REPO_TYPE,
        commit_message=commit_message,
        parent_commit=parent_commit,
    )


__all__ = [
    "DATASET_REPO_TYPE",
    "commit_operation_add",
    "create_dataset_commit",
    "new_hf_api",
]
