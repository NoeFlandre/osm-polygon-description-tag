"""Shared complete publication datasets for unit tests."""

from collections.abc import Callable
from pathlib import Path

import pytest

from tests.helpers.publication import build_publication_dataset


@pytest.fixture
def publication_dataset_factory() -> Callable[[Path], None]:
    """Return the shared builder so tests can keep choosing their own root."""
    return build_publication_dataset
