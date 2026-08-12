"""Keep per-source orchestration in its focused workflow module."""

from osm_polygon_description_tag.workflow import orchestrator, source_runner


def test_orchestrator_delegates_per_source_state_machine() -> None:
    """The run-level orchestrator reuses the canonical source runner."""
    assert orchestrator.SourceOutcome is source_runner.SourceOutcome
    assert orchestrator._local_artifact_is_complete is source_runner.local_artifact_is_complete
    assert orchestrator._process_one is source_runner.process_one
    assert orchestrator._published_state_matches is source_runner.published_state_matches
