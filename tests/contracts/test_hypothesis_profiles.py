"""Hypothesis runs deterministically in CI and under mutation testing (#78)."""

from __future__ import annotations

from pathlib import Path

from hypothesis import settings

from tests import conftest

WORKFLOW = Path(__file__).resolve().parents[2] / ".github" / "workflows" / "quality.yml"


def test_ci_and_mutation_profiles_are_deterministic() -> None:
    for name in ("ci", "mutation"):
        profile = settings.get_profile(name)
        assert profile.derandomize is True
        assert profile.deadline is None
        assert profile.database is None
    assert settings.get_profile("mutation").max_examples == 30
    assert settings.get_profile("ci").max_examples == 100


def test_profile_selection(monkeypatch) -> None:
    monkeypatch.delenv("HYPOTHESIS_PROFILE", raising=False)
    monkeypatch.delenv("MUTANT_UNDER_TEST", raising=False)
    assert conftest._hypothesis_profile() == "dev"
    monkeypatch.setenv("MUTANT_UNDER_TEST", "pkg.mod.x_f__mutmut_1")
    assert conftest._hypothesis_profile() == "mutation"
    monkeypatch.setenv("HYPOTHESIS_PROFILE", "ci")
    assert conftest._hypothesis_profile() == "ci"


def test_the_workflow_selects_a_profile_for_every_test_job() -> None:
    workflow = WORKFLOW.read_text(encoding="utf-8")

    assert workflow.count("HYPOTHESIS_PROFILE: ci") == 2
    assert workflow.count("HYPOTHESIS_PROFILE: mutation") == 3
