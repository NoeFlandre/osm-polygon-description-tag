"""Exact staging, policy-capture, and import contracts of the Grid'5000 operator.

A staged payload is reused across submissions of the same shard, and a retrieved
one is merged back into authoritative local state. Both steps have to bind to the
one bundle they belong to, and the evidence capture that gates them has to carry
the caller's own timeout so a hung frontend command cannot stall a rollout.
"""

from __future__ import annotations

import tempfile
from pathlib import Path
from types import SimpleNamespace

import pytest

from osm_polygon_description_tag.workflow import grid_operator as operator
from osm_polygon_description_tag.workflow.grid_operator import (
    GridOperatorError,
    prepare_portable_job,
)
from osm_polygon_description_tag.workflow.grid_scheduler import CommandResult, SchedulerError
from tests.helpers.messages import exactly
from tests.helpers.sentences import REMOTE_SAT_MODEL_PATH
from tests.unit.workflow.test_grid_operator import SHARD

_REMOTE_BUNDLE = "/scratch/lang-bundle"


def _ok(stdout: str) -> CommandResult:
    return CommandResult(("cmd",), returncode=0, stdout=stdout, stderr="")


def _recording_runner(results: dict[str, CommandResult]) -> object:
    seen: list[tuple[tuple[str, ...], float]] = []

    def run(argv: tuple[str, ...], timeout: float) -> CommandResult:
        seen.append((argv, timeout))
        return results[argv[0]]

    run.seen = seen  # type: ignore[attr-defined]
    return run


def test_policy_capture_sends_the_documented_argv_with_the_callers_timeout() -> None:
    """A dropped timeout lets one hung frontend command stall the whole rollout."""
    runner = _recording_runner(
        {"usagepolicycheck": _ok('{"jobs": []}'), "quota": _ok("quota output")}
    )

    usage, quota = operator.gather_policy_outputs("nancy", runner=runner, timeout=7.5)  # type: ignore[arg-type]

    assert usage == '{"jobs": []}'
    assert quota == "quota output"
    assert runner.seen == [  # type: ignore[attr-defined]
        (("usagepolicycheck", "-t", "--sites", "nancy", "--json"), 7.5),
        (("quota", "-p", "-w"), 7.5),
    ]


def test_account_evidence_capture_also_carries_the_callers_timeout() -> None:
    runner = _recording_runner(
        {
            "usagepolicycheck": _ok('{"jobs": []}'),
            "quota": _ok("quota output"),
            "oarstat": _ok("{}"),
        }
    )

    operator.gather_policy_evidence("nancy", runner=runner, timeout=2.5)  # type: ignore[arg-type]

    assert [timeout for _argv, timeout in runner.seen] == [2.5, 2.5, 2.5]  # type: ignore[attr-defined]
    assert runner.seen[2][0] == ("oarstat", "-u", "-J")  # type: ignore[attr-defined]


def test_a_hyphenated_site_name_is_accepted() -> None:
    """Grid'5000 site names may carry a hyphen, and refusing one blocks a whole site."""
    runner = _recording_runner({"usagepolicycheck": _ok("{}"), "quota": _ok("")})

    operator.gather_policy_outputs("nancy-2", runner=runner, timeout=1.0)  # type: ignore[arg-type]

    assert runner.seen[0][0][3] == "nancy-2"  # type: ignore[attr-defined]


@pytest.mark.parametrize("site", ["", "---", "nancy/../etc", "nancy 2"])
def test_a_site_name_that_is_not_simply_alphanumeric_is_refused_exactly(site: str) -> None:
    """Everything but letters, digits and hyphens reaches a shell on the frontend."""
    with pytest.raises(GridOperatorError, match=exactly("site must be a simple alphanumeric name")):
        operator.gather_policy_outputs(site)


def test_an_unavailable_scheduler_command_is_unknown_evidence_not_approval() -> None:
    def refuse(_argv: tuple[str, ...], _timeout: float) -> CommandResult:
        raise SchedulerError("scheduler executable is not available: quota")

    assert operator._captured(("quota", "-p", "-w"), refuse, 1.0) is None  # type: ignore[arg-type]


def test_collection_validates_only_the_shard_it_was_asked_about(
    request: pytest.FixtureRequest, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Validating every shard would report another shard's problem against this one."""
    _, run, _ = request.getfixturevalue("prepared")
    seen: list[object] = []

    def fake_validate_run(run_dir: Path, *, shards: object = None) -> object:
        seen.append((run_dir, shards))
        return SimpleNamespace()

    monkeypatch.setattr(operator, "validate_run", fake_validate_run)

    operator.collect_results(run, SHARD)

    assert seen == [(run, (SHARD,))]


def test_a_payload_is_materialised_inside_the_job_directory(
    request: pytest.FixtureRequest, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A temporary elsewhere could not be renamed into place atomically."""
    project, source, run, snapshot = request.getfixturevalue("portable_prepared")
    seen: list[object] = []
    real_temporary_directory = tempfile.TemporaryDirectory

    def recording_temporary_directory(*args: object, **kwargs: object) -> object:
        seen.append(kwargs)
        return real_temporary_directory(*args, **kwargs)  # type: ignore[arg-type]

    monkeypatch.setattr(operator.tempfile, "TemporaryDirectory", recording_temporary_directory)

    prepared_job = prepare_portable_job(
        run,
        project,
        source,
        snapshot,
        SHARD,
        remote_bundle_dir=_REMOTE_BUNDLE,
        sat_model_path=REMOTE_SAT_MODEL_PATH,
    )

    assert seen == [{"prefix": ".payload-", "dir": prepared_job.paths.root}]


def test_a_staged_payload_is_verified_against_the_bundle_it_was_staged_for(
    request: pytest.FixtureRequest,
) -> None:
    """Reusing another shard's payload would run the wrong input on the node."""
    project, source, run, snapshot = request.getfixturevalue("portable_prepared")
    prepared_job = prepare_portable_job(
        run,
        project,
        source,
        snapshot,
        SHARD,
        remote_bundle_dir=_REMOTE_BUNDLE,
        sat_model_path=REMOTE_SAT_MODEL_PATH,
    )
    foreign = operator.bundle_for_shard(snapshot, SHARD)
    foreign = type(foreign)(
        snapshot_id="f" * 64,
        model_config_fingerprint=foreign.model_config_fingerprint,
        code_fingerprint=foreign.code_fingerprint,
        lock_fingerprint=foreign.lock_fingerprint,
        shard=foreign.shard,
        source_sha256=foreign.source_sha256,
        source_size_bytes=foreign.source_size_bytes,
        input_row_count=foreign.input_row_count,
    )
    fingerprint = operator._staged_resume_fingerprint(prepared_job.payload_root)

    with pytest.raises(GridOperatorError) as caught:
        operator._existing_payload_for_resume(
            prepared_job.payload_root,
            foreign,
            fingerprint,  # type: ignore[arg-type]
        )

    assert str(caught.value) == "staged bundle does not match the expected bundle"


@pytest.mark.parametrize(
    ("value", "message"),
    [
        ("", "remote bundle directory must be a non-empty string"),
        ("relative/path", "remote bundle directory must be an absolute path"),
        ("/has space", "remote bundle directory must not contain shell metacharacters"),
        ("/a/../b", "remote bundle directory must not contain traversal components"),
    ],
)
def test_an_unusable_remote_bundle_directory_is_refused_under_its_own_label(
    request: pytest.FixtureRequest, value: str, message: str
) -> None:
    """The label tells the operator which of the four remote paths they got wrong."""
    project, source, run, snapshot = request.getfixturevalue("portable_prepared")

    with pytest.raises(GridOperatorError, match=exactly(message)):
        prepare_portable_job(
            run,
            project,
            source,
            snapshot,
            SHARD,
            remote_bundle_dir=value,
            sat_model_path=REMOTE_SAT_MODEL_PATH,
        )


def test_a_retrieved_run_directory_that_is_not_a_directory_is_named_in_full(
    tmp_path: Path,
) -> None:
    retrieved = tmp_path / "retrieved"
    retrieved.write_text("not a directory", encoding="utf-8")

    with pytest.raises(
        GridOperatorError,
        match=exactly(f"retrieved run directory is not a regular directory: {retrieved}"),
    ):
        operator.import_retrieved_results(tmp_path / "run", retrieved, SHARD)


@pytest.mark.parametrize(
    ("shard", "message"),
    [
        ("", "shard must be a non-empty relative path"),
        ("region.csv", "shard must be a Parquet path"),
    ],
)
def test_an_unusable_shard_is_refused_before_any_directory_is_read(
    tmp_path: Path, shard: str, message: str
) -> None:
    with pytest.raises(GridOperatorError, match=exactly(message)):
        operator.import_retrieved_results(tmp_path / "run", tmp_path / "retrieved", shard)


def test_the_payload_root_is_selected_for_the_bundle_being_staged(
    request: pytest.FixtureRequest, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Selecting without the bundle would reuse another shard's staged payload."""
    project, source, run, snapshot = request.getfixturevalue("portable_prepared")
    seen: list[object] = []
    real = operator._existing_payload_for_resume

    def recording(payload_root: Path, bundle: object, fingerprint: str) -> object:
        seen.append(bundle)
        return real(payload_root, bundle, fingerprint)  # type: ignore[arg-type]

    monkeypatch.setattr(operator, "_existing_payload_for_resume", recording)

    prepared_job = prepare_portable_job(
        run,
        project,
        source,
        snapshot,
        SHARD,
        remote_bundle_dir=_REMOTE_BUNDLE,
        sat_model_path=REMOTE_SAT_MODEL_PATH,
    )

    assert seen
    assert all(bundle == prepared_job.bundle for bundle in seen)


def test_a_prepared_view_describes_the_job_and_bundle_that_were_staged(
    request: pytest.FixtureRequest, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The view is what the caller transfers, so it must name this job's own paths."""
    project, source, run, snapshot = request.getfixturevalue("portable_prepared")
    seen: list[object] = []
    real = operator._prepared_view

    def recording(paths: object, bundle: object, payload_root: Path) -> object:
        seen.append((paths, bundle))
        return real(paths, bundle, payload_root)  # type: ignore[arg-type]

    monkeypatch.setattr(operator, "_prepared_view", recording)

    prepare_portable_job(
        run,
        project,
        source,
        snapshot,
        SHARD,
        remote_bundle_dir=_REMOTE_BUNDLE,
        sat_model_path=REMOTE_SAT_MODEL_PATH,
    )

    expected_bundle = operator.bundle_for_shard(snapshot, SHARD)
    assert seen == [(operator.job_paths(run, expected_bundle), expected_bundle)]


def test_a_portable_job_carries_the_documented_default_batch_size(
    request: pytest.FixtureRequest,
) -> None:
    project, source, run, snapshot = request.getfixturevalue("portable_prepared")

    prepared_job = prepare_portable_job(
        run,
        project,
        source,
        snapshot,
        SHARD,
        remote_bundle_dir=_REMOTE_BUNDLE,
        sat_model_path=REMOTE_SAT_MODEL_PATH,
    )

    script = (prepared_job.payload_root / operator.JOB_SCRIPT_FILENAME).read_text(encoding="utf-8")
    assert "  --batch-size 512 \\\n" in script


def test_a_staged_payload_whose_snapshot_is_unreadable_reports_the_readers_words(
    request: pytest.FixtureRequest,
) -> None:
    """Replacing the cause with ``None`` would report the string ``None``."""
    project, source, run, snapshot = request.getfixturevalue("portable_prepared")
    prepared_job = prepare_portable_job(
        run,
        project,
        source,
        snapshot,
        SHARD,
        remote_bundle_dir=_REMOTE_BUNDLE,
        sat_model_path=REMOTE_SAT_MODEL_PATH,
    )
    run_root = prepared_job.payload_root / operator.STAGE_RUN_DIRNAME
    (run_root / operator.SNAPSHOT_FILENAME).write_text("{not json", encoding="utf-8")

    with pytest.raises(GridOperatorError) as caught:
        operator._verify_payload_identity(
            prepared_job.payload_root / operator.STAGE_PROJECT_DIRNAME,
            prepared_job.payload_root / operator.STAGE_SOURCE_DIRNAME,
            run_root,
            prepared_job.bundle,
        )

    assert str(caught.value).startswith("cannot read snapshot ")


@pytest.mark.parametrize(
    ("remote_bundle_dir", "base"),
    [
        ("/scratch/lang-bundle", "/scratch/lang-bundle"),
        ("/scratch/lang-bundle///", "/scratch/lang-bundle"),
        ("/scratch/lang-bundleX", "/scratch/lang-bundleX"),
        ("/", "/"),
        ("///", "/"),
    ],
)
def test_a_portable_job_normalises_its_remote_bundle_root_once(
    request: pytest.FixtureRequest, remote_bundle_dir: str, base: str
) -> None:
    """These three remote children are baked into the script the node executes."""
    project, source, run, snapshot = request.getfixturevalue("portable_prepared")

    prepared_job = prepare_portable_job(
        run,
        project,
        source,
        snapshot,
        SHARD,
        remote_bundle_dir=remote_bundle_dir,
        sat_model_path=REMOTE_SAT_MODEL_PATH,
    )

    script = (prepared_job.payload_root / operator.JOB_SCRIPT_FILENAME).read_text(encoding="utf-8")
    project_child = "/project" if base == "/" else f"{base}/project"
    run_child = "/run" if base == "/" else f"{base}/run"
    assert f"cd {project_child}\n" in script
    assert f"  --run-dir {run_child} \\\n" in script


def test_a_retrieved_shard_with_collection_issues_is_named_in_the_refusal(
    request: pytest.FixtureRequest,
) -> None:
    """The operator has to know which of the 386 shards came back damaged."""
    from osm_polygon_description_tag.dataset.languages.checkpoint import shard_paths
    from tests.unit.workflow.test_grid_operator import _process_collection_fixture

    source, local, incoming, snapshot = request.getfixturevalue("collection_runs")
    _process_collection_fixture(source, incoming, snapshot)
    part = next(iter(sorted(shard_paths(incoming, SHARD).parts.iterdir())))
    part.write_bytes(b"corrupted")

    with pytest.raises(
        GridOperatorError,
        match=exactly(f"retrieved {SHARD} has collection validation issues"),
    ):
        operator.import_retrieved_results(local, incoming, SHARD)


@pytest.mark.parametrize("nesting", ["source_inside_destination", "destination_inside_source"])
def test_a_retrieved_directory_nested_in_the_local_one_is_refused_either_way(
    tmp_path: Path, nesting: str
) -> None:
    """Copying between nested directories would overwrite the state being read."""
    outer = tmp_path / "outer"
    inner = outer / "inner"
    inner.mkdir(parents=True)
    if nesting == "source_inside_destination":
        source, destination = inner, outer
    else:
        source, destination = outer, inner

    with pytest.raises(
        GridOperatorError,
        match=exactly("retrieved and local shard directories must be distinct"),
    ):
        operator._reject_overlapping_paths(source, destination)


def test_two_separate_shard_directories_are_accepted(tmp_path: Path) -> None:
    """Only nesting is refused; two ordinary directories must pass."""
    source = tmp_path / "retrieved"
    destination = tmp_path / "local"
    source.mkdir()
    destination.mkdir()

    assert operator._reject_overlapping_paths(source, destination) is None


def test_restaging_an_unchanged_payload_reports_this_jobs_own_paths(
    request: pytest.FixtureRequest, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The second staging returns early; it must still describe this job."""
    project, source, run, snapshot = request.getfixturevalue("portable_prepared")
    first = prepare_portable_job(
        run,
        project,
        source,
        snapshot,
        SHARD,
        remote_bundle_dir=_REMOTE_BUNDLE,
        sat_model_path=REMOTE_SAT_MODEL_PATH,
    )
    seen: list[object] = []
    real = operator._prepared_view

    def recording(paths: object, bundle: object, payload_root: Path) -> object:
        seen.append((paths, bundle))
        return real(paths, bundle, payload_root)  # type: ignore[arg-type]

    monkeypatch.setattr(operator, "_prepared_view", recording)

    second = prepare_portable_job(
        run,
        project,
        source,
        snapshot,
        SHARD,
        remote_bundle_dir=_REMOTE_BUNDLE,
        sat_model_path=REMOTE_SAT_MODEL_PATH,
    )

    expected_bundle = operator.bundle_for_shard(snapshot, SHARD)
    assert second.payload_root == first.payload_root
    assert seen == [(operator.job_paths(run, expected_bundle), expected_bundle)]


def test_a_processing_budget_that_does_not_fit_the_walltime_is_refused_when_preparing(
    request: pytest.FixtureRequest,
) -> None:
    """The walltime the caller asked for is what the budget has to fit inside."""
    _, run, snapshot = request.getfixturevalue("prepared")

    with pytest.raises(
        GridOperatorError, match=exactly("processing budget must be less than walltime")
    ):
        operator.prepare_job(
            run,
            snapshot,
            SHARD,
            remote_project_dir="/home/user/project",
            remote_source_dir="/scratch/staging/source",
            remote_run_dir="/scratch/staging/run",
            processing_seconds=1000,
            batch_size=64,
            walltime_seconds=900,
            sat_model_path=REMOTE_SAT_MODEL_PATH,
        )
