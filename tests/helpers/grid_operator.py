"""Canonical prepared source pair for Grid operator tests."""

import json
from collections.abc import Callable, Sequence
from datetime import UTC, datetime
from pathlib import Path

from shapely.geometry import Polygon

from osm_polygon_description_tag.dataset.languages.snapshot import (
    SnapshotManifest,
    prepare_snapshot,
)
from osm_polygon_description_tag.dataset.storage import write_geoparquet
from osm_polygon_description_tag.workflow.grid_operator import (
    JobBundle,
    JobPaths,
    SubmissionIntent,
)
from osm_polygon_description_tag.workflow.grid_policy import (
    MAX_WALLTIME_SECONDS,
    PolicyDecision,
    PolicyEvidence,
    PolicyVerdict,
)
from osm_polygon_description_tag.workflow.grid_scheduler import CommandResult
from tests.conftest import make_record_dict


def prepared_two_shard_run(
    root: Path, primary_shard: str = "region.parquet"
) -> tuple[Path, SnapshotManifest, dict[str, Path]]:
    """Write two two-row shards and pin them in a synthetic immutable snapshot."""
    source = root / "source"
    source.mkdir(parents=True)
    for shard in (primary_shard, "other.parquet"):
        write_geoparquet(
            (
                make_record_dict(
                    Polygon([(0, 0), (0, 1), (1, 1), (1, 0)]),
                    {"description": f"A synthetic description {index}"},
                    osm_id=index + 1,
                )
                for index in range(2)
            ),
            source / shard,
            batch_size=1,
        )
    run = root / "run"
    snapshot = prepare_snapshot(source, run, code_fingerprint="a" * 64, lock_fingerprint="b" * 64)
    return run, snapshot, {"source": source, "run": run}


def allowed_policy_verdict(
    decision: PolicyDecision = PolicyDecision.ALLOWED, *, captured_at: datetime | None = None
) -> PolicyVerdict:
    """Build the default positive policy evidence used by operator tests."""
    return PolicyVerdict(
        decision,
        ("test verdict",),
        PolicyEvidence(0, False, True, (), captured_at=captured_at or datetime.now(UTC)),
    )


def queued_command_runner(
    results: list[CommandResult], observed: list[tuple[str, ...]] | None = None
) -> Callable[[Sequence[str], float], CommandResult]:
    """Return an ordered command stub and optionally record each argv tuple."""

    def run(argv: Sequence[str], timeout: float) -> CommandResult:
        del timeout
        if observed is not None:
            observed.append(tuple(argv))
        return results.pop(0)

    return run


def write_submitted_intent(paths: JobPaths, bundle: JobBundle) -> None:
    """Record the acknowledged terminal attempt used by retry-planning tests."""
    intent = SubmissionIntent(
        bundle_id=bundle.bundle_id,
        shard=bundle.shard,
        job_name=f"lang-{bundle.bundle_id[:16]}",
        walltime_seconds=MAX_WALLTIME_SECONDS,
        cores=1,
        recorded_at="2026-09-09T21:00:00+00:00",
        job_id=6917617,
        outcome="submitted",
        terminal_state="terminated",
        reconciled_at="2026-09-09T21:05:00+00:00",
        result_acknowledged=True,
    )
    paths.intent.write_text(json.dumps(intent.to_payload()), encoding="utf-8")
