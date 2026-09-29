"""Shared language dataset setup fixtures."""

import pytest

from tests.helpers.language_setup import LanguageRunSetup


@pytest.fixture
def language_run_setup() -> LanguageRunSetup:
    """Build the canonical source shard and prepared language run for tests."""
    return LanguageRunSetup()
