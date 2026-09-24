"""Adverse boundaries of the ``language grid`` handlers.

These contracts are only reachable through error paths or private helpers, so
they are exercised directly. Nothing here contacts a scheduler, a transport, or
the Hub.
"""

import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from osm_polygon_description_tag import language_cli
from osm_polygon_description_tag.workflow import grid_commands
from osm_polygon_description_tag.workflow.grid_operator import GridOperatorError
from osm_polygon_description_tag.workflow.grid_scheduler import SchedulerError
from tests.helpers.messages import exactly

SHARD = "region.parquet"


def _stdout(capsys: pytest.CaptureFixture[str]) -> dict[str, object]:
    return json.loads(capsys.readouterr().out)


def test_missing_account_evidence_leaves_the_job_count_unknown(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        grid_commands, "gather_policy_evidence", lambda site, runner: ("usage", "quota", None)
    )

    usage, quota, count, captured_at = grid_commands._capture_policy("nancy", runner=None)

    assert (usage, quota, count) == ("usage", "quota", None)
    assert captured_at.tzinfo is not None


def test_invalid_account_evidence_fails_closed(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        grid_commands, "gather_policy_evidence", lambda site, runner: ("usage", "quota", "garbage")
    )

    def refuse(output: str) -> int:
        raise SchedulerError("unreadable job map")

    monkeypatch.setattr(grid_commands, "account_active_job_count", refuse)

    with pytest.raises(GridOperatorError, match="account-wide scheduler evidence is invalid"):
        grid_commands._capture_policy("nancy", runner=None)


def test_no_payload_candidates_without_a_job_directory(tmp_path: Path) -> None:
    paths = SimpleNamespace(root=tmp_path / "absent", payload_root=tmp_path / "absent" / "payload")

    assert grid_commands._portable_payload_candidates(paths) == ()  # type: ignore[arg-type]


def test_a_symlinked_job_directory_is_rejected(tmp_path: Path) -> None:
    root = tmp_path / "job"
    root.symlink_to(tmp_path / "elsewhere", target_is_directory=True)

    with pytest.raises(GridOperatorError, match="must not be a symlink"):
        grid_commands._existing_job_directory(root)


def test_a_job_path_that_is_not_a_directory_is_rejected(tmp_path: Path) -> None:
    root = tmp_path / "job"
    root.write_text("not a directory\n", encoding="utf-8")

    with pytest.raises(GridOperatorError, match="not a regular directory"):
        grid_commands._existing_job_directory(root)


def test_an_absent_job_directory_is_reported_as_missing(tmp_path: Path) -> None:
    assert grid_commands._existing_job_directory(tmp_path / "absent") is None


def test_non_sibling_remote_paths_cannot_infer_a_portable_root() -> None:
    with pytest.raises(
        GridOperatorError,
        match=exactly(
            "staged portable submission requires sibling remote project/source/run paths"
        ),
    ):
        grid_commands._infer_remote_bundle_dir(
            "/scratch/bundle/project", "/other/place/source", "/scratch/bundle/run"
        )


@pytest.mark.parametrize("defect", ["missing", "different", "symlink"])
def test_a_local_artifact_that_differs_from_the_payload_is_rejected(
    tmp_path: Path, defect: str
) -> None:
    job = tmp_path / "job"
    payload = job / "payload"
    payload.mkdir(parents=True)
    names = ("bundle.json", "job-config.json", "job.sh")
    for name in names:
        (payload / name).write_text(f"{name} staged\n", encoding="utf-8")
        (job / name).write_text(f"{name} staged\n", encoding="utf-8")
    paths = SimpleNamespace(
        bundle=job / "bundle.json", config=job / "job-config.json", script=job / "job.sh"
    )
    if defect == "missing":
        paths.bundle.unlink()
    elif defect == "different":
        paths.bundle.write_text("local drift\n", encoding="utf-8")
    else:
        paths.bundle.unlink()
        paths.bundle.symlink_to(payload / "bundle.json")

    with pytest.raises(GridOperatorError, match="differs from staged payload"):
        grid_commands._verify_local_staged_files(paths, payload)  # type: ignore[arg-type]


def test_seeding_keeps_an_existing_retrieved_snapshot(tmp_path: Path) -> None:
    local = tmp_path / "run"
    local.mkdir()
    (local / "snapshot.json").write_text('{"local": true}\n', encoding="utf-8")
    retrieved = tmp_path / "retrieved"
    retrieved.mkdir()
    (retrieved / "snapshot.json").write_text('{"already": true}\n', encoding="utf-8")

    grid_commands._seed_retrieval_snapshot(local, retrieved)

    assert (retrieved / "snapshot.json").read_text(encoding="utf-8") == '{"already": true}\n'


def test_remote_collection_requires_a_local_staging_directory(tmp_path: Path) -> None:
    with pytest.raises(
        GridOperatorError,
        match=exactly("remote collection requires --retrieved-run-dir for its local staging area"),
    ):
        language_cli.handle_grid_collect(
            tmp_path / "run", SHARD, remote_bundle_dir="/scratch/bundle"
        )


def test_collecting_an_already_retrieved_run_imports_and_acknowledges(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    run_dir = tmp_path / "run"
    retrieved_run_dir = tmp_path / "retrieved"
    paths = object()
    report = SimpleNamespace(to_payload=lambda: {"complete": False, "annotation_count": 2})
    acknowledgment = SimpleNamespace(
        to_payload=lambda: {"result_acknowledged": True, "result_complete": False}
    )
    observed: list[object] = []

    def fake_import(local_run: Path, incoming: Path, shard: str) -> object:
        observed.append(("import", local_run, incoming, shard))
        return report

    def fake_ack(received_paths: object, received_report: object) -> object:
        observed.append(("ack", received_paths, received_report))
        return acknowledgment

    monkeypatch.setattr(grid_commands, "import_retrieved_results", fake_import, raising=False)
    monkeypatch.setattr(grid_commands, "acknowledge_collected_results", fake_ack, raising=False)
    monkeypatch.setattr(grid_commands, "read_snapshot", lambda _: object())
    monkeypatch.setattr(grid_commands, "bundle_for_shard", lambda *_: object())
    monkeypatch.setattr(grid_commands, "job_paths", lambda *_: paths)
    monkeypatch.setattr(
        grid_commands,
        "adopt_retrieved_intent",
        lambda received_paths, incoming, _bundle: observed.append(
            ("adopt", received_paths, incoming)
        ),
        raising=False,
    )

    language_cli.handle_grid_collect(run_dir, SHARD, retrieved_run_dir=retrieved_run_dir)

    assert observed == [
        ("import", run_dir, retrieved_run_dir, SHARD),
        ("adopt", paths, retrieved_run_dir),
        ("ack", paths, report),
    ]
    assert _stdout(capsys) == {
        "acknowledgment": {"result_acknowledged": True, "result_complete": False},
        "annotation_count": 2,
        "applied": False,
        "complete": False,
        "retrieved_run_dir": str(retrieved_run_dir),
        "run_dir": str(run_dir),
        "shard": SHARD,
        "transfer_argv": None,
        "transfer_result": None,
    }
