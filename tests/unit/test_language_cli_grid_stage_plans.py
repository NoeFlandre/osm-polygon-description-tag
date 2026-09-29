"""End-to-end behaviour of the ``language`` command group on synthetic data."""

import json
import os
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace

import pytest
from shapely.geometry import Polygon

from osm_polygon_description_tag import language_cli
from osm_polygon_description_tag.cli import run
from osm_polygon_description_tag.dataset.storage import write_geoparquet
from tests.conftest import make_record_dict
from tests.helpers.sentences import REMOTE_SAT_MODEL_PATH, fake_splitter

SAT_MODEL_PATH = "/models/sat-3l-sm/model.safetensors"


SHARD = "region.parquet"


NIGHT_INSTANT = datetime(2026, 9, 8, 20, 0, tzinfo=UTC)


@pytest.fixture(autouse=True)
def _fake_sentence_splitter(monkeypatch: pytest.MonkeyPatch) -> None:
    """Keep the CLI tests about wiring; the real adapter has its own tests."""
    monkeypatch.setattr(language_cli, "build_sat_splitter", lambda *, model_dir: fake_splitter())


@pytest.fixture
def night_clock(monkeypatch: pytest.MonkeyPatch) -> datetime:
    """Freeze the Grid handlers' clock so the day/night policy is deterministic."""
    monkeypatch.setattr(language_cli, "_utc_now", lambda: NIGHT_INSTANT)
    return NIGHT_INSTANT


@pytest.fixture
def project(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    root = tmp_path / "project"
    (root / "src").mkdir(parents=True)
    (root / "src" / "example.py").write_text("VALUE = 1\n", encoding="utf-8")
    (root / "pyproject.toml").write_text(
        "[project]\nname = 'synthetic-language-project'\nversion = '0.0.0'\n",
        encoding="utf-8",
    )
    (root / "uv.lock").write_text("lock = 1\n", encoding="utf-8")
    monkeypatch.chdir(root)
    return root


@pytest.fixture
def source(tmp_path: Path) -> Path:
    root = tmp_path / "source"
    records = (
        make_record_dict(
            Polygon([(0, 0), (0, 1), (1, 1), (1, 0)]),
            {
                "description": f"A synthetic description {index}",
                "description:fr": f"Le mur {index}",
            },
            osm_id=index + 1,
        )
        for index in range(6)
    )
    root.mkdir(parents=True)
    write_geoparquet(records, root / SHARD, batch_size=3)
    return root


def _stdout(capsys: pytest.CaptureFixture[str]) -> dict[str, object]:
    captured = capsys.readouterr()
    assert captured.err == ""
    return json.loads(captured.out)


def _prepare_run(tmp_path: Path, project: Path, source: Path) -> Path:
    run_dir = tmp_path / "run"
    assert (
        run(
            [
                "language",
                "prepare",
                "--source-root",
                str(source),
                "--run-dir",
                str(run_dir),
                "--project-root",
                str(project),
            ]
        )
        == 0
    )
    return run_dir


_REMOTE = [
    "--remote-project-dir",
    "/home/user/project",
    "--remote-source-dir",
    "/scratch/staging/source",
    "--remote-run-dir",
    "/scratch/staging/run",
    "--glotlid-model-path",
    "/home/user/models/glotlid-v3/model_v3.bin",
    "--sat-model-path",
    SAT_MODEL_PATH,
]


def test_grid_stage_plans_a_portable_transfer_without_the_apply_gate(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    run_dir = tmp_path / "run"
    project_root = tmp_path / "project"
    source_root = tmp_path / "source"
    snapshot = object()
    prepared = SimpleNamespace(
        bundle=SimpleNamespace(bundle_id="bundle-1", shard=SHARD, input_row_count=6),
        payload_root=tmp_path / "payload",
        manifest=tmp_path / "payload" / "stage.json",
    )
    transfer_argv = (
        "rsync",
        "--archive",
        "--protect-args",
        "--",
        "/local/payload/",
        "/scratch/bundle/",
    )
    calls: dict[str, object] = {}

    monkeypatch.setattr(language_cli, "read_snapshot", lambda _: snapshot)

    def fake_prepare_portable_job(*args: object, **kwargs: object) -> object:
        calls["prepare"] = (args, kwargs)
        return prepared

    def fake_build_transfer(received_prepared: object, remote_bundle_dir: str) -> tuple[str, ...]:
        calls["transfer"] = (received_prepared, remote_bundle_dir)
        return transfer_argv

    monkeypatch.setattr(language_cli, "prepare_portable_job", fake_prepare_portable_job)
    monkeypatch.setattr(language_cli, "build_bundle_transfer_argv", fake_build_transfer)

    language_cli.handle_grid_stage(
        run_dir=run_dir,
        project_root=project_root,
        source_root=source_root,
        shard=SHARD,
        remote_bundle_dir="/scratch/bundle",
        processing_seconds=1200,
        batch_size=512,
        walltime_seconds=1800,
        apply=False,
        sat_model_path=REMOTE_SAT_MODEL_PATH,
    )

    assert calls["prepare"] == (
        (run_dir, project_root, source_root, snapshot, SHARD),
        {
            "remote_bundle_dir": "/scratch/bundle",
            "sat_model_path": REMOTE_SAT_MODEL_PATH,
            "processing_seconds": 1200,
            "batch_size": 512,
            "walltime_seconds": 1800,
            "glotlid_model_path": None,
        },
    )
    assert calls["transfer"] == (prepared, "/scratch/bundle")
    assert _stdout(capsys) == {
        "applied": False,
        "bundle_id": "bundle-1",
        "input_row_count": 6,
        "manifest": str(prepared.manifest),
        "payload_dir": str(prepared.payload_root),
        "processing_seconds": 1200,
        "quarantined": [],
        "batch_size": 512,
        "walltime_seconds": 1800,
        "remote_bundle_dir": "/scratch/bundle",
        "remote_project_dir": "/scratch/bundle/project",
        "remote_run_dir": "/scratch/bundle/run",
        "remote_source_dir": "/scratch/bundle/source",
        "run_dir": str(run_dir),
        "shard": SHARD,
        "transfer_argv": list(transfer_argv),
        "transfer_result": None,
    }


def test_grid_stage_apply_executes_only_the_explicit_transfer_argv(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    run_dir = tmp_path / "run"
    prepared = SimpleNamespace(
        bundle=SimpleNamespace(bundle_id="bundle-2", shard=SHARD, input_row_count=3),
        payload_root=tmp_path / "payload",
        manifest=tmp_path / "payload" / "stage.json",
    )
    transfer_argv = ("rsync", "--archive", "--", "/payload/", "/remote/")
    observed: list[tuple[tuple[str, ...], float]] = []

    monkeypatch.setattr(language_cli, "read_snapshot", lambda _: object())
    monkeypatch.setattr(language_cli, "prepare_portable_job", lambda *_, **__: prepared)
    monkeypatch.setattr(language_cli, "build_bundle_transfer_argv", lambda *_: transfer_argv)

    def fake_runner(argv: tuple[str, ...], timeout: float) -> SimpleNamespace:
        observed.append((argv, timeout))
        return SimpleNamespace(returncode=0, stdout="transferred\n", stderr="", timed_out=False)

    language_cli.handle_grid_stage(
        run_dir=run_dir,
        project_root=tmp_path / "project",
        source_root=tmp_path / "source",
        shard=SHARD,
        remote_bundle_dir="/remote",
        processing_seconds=1200,
        batch_size=512,
        walltime_seconds=1800,
        apply=True,
        runner=fake_runner,
        sat_model_path=REMOTE_SAT_MODEL_PATH,
    )

    payload = _stdout(capsys)
    assert observed == [(transfer_argv, 120.0)]
    assert payload["applied"] is True
    assert payload["transfer_argv"] == list(transfer_argv)
    assert payload["transfer_result"] == {
        "argv": list(transfer_argv),
        "returncode": 0,
        "stderr": "",
        "stdout": "transferred\n",
        "timed_out": False,
    }


@pytest.mark.parametrize(
    ("outcome", "message"),
    [
        (language_cli.SchedulerError("missing rsync"), "transport command could not run"),
        (SimpleNamespace(returncode=-1, stdout="", stderr="", timed_out=True), "timed out"),
        (
            SimpleNamespace(returncode=23, stdout="", stderr="permission denied", timed_out=False),
            "exited 23: permission denied",
        ),
        (
            SimpleNamespace(returncode=23, stdout="", stderr="", timed_out=False),
            "exited 23: no diagnostic",
        ),
    ],
)
def test_grid_stage_reports_transport_failures(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    outcome: object,
    message: str,
) -> None:
    prepared = SimpleNamespace(
        bundle=SimpleNamespace(bundle_id="bundle-failure", shard=SHARD, input_row_count=1),
        payload_root=tmp_path / "payload",
        manifest=tmp_path / "payload" / "stage.json",
    )
    transfer_argv = ("rsync", "--archive", "--", "/payload/", "/remote/")
    monkeypatch.setattr(language_cli, "read_snapshot", lambda _: object())
    monkeypatch.setattr(language_cli, "prepare_portable_job", lambda *_, **__: prepared)
    monkeypatch.setattr(language_cli, "build_bundle_transfer_argv", lambda *_: transfer_argv)

    def failing_runner(argv: tuple[str, ...], timeout: float) -> object:
        if isinstance(outcome, BaseException):
            raise outcome
        return outcome

    with pytest.raises(language_cli.GridOperatorError, match=message):
        language_cli.handle_grid_stage(
            run_dir=tmp_path / "run",
            project_root=tmp_path / "project",
            source_root=tmp_path / "source",
            shard=SHARD,
            remote_bundle_dir="/remote",
            processing_seconds=1200,
            batch_size=512,
            walltime_seconds=1800,
            apply=True,
            runner=failing_runner,  # type: ignore[arg-type],
            sat_model_path=REMOTE_SAT_MODEL_PATH,
        )


def test_grid_submit_passes_fresh_account_wide_policy_evidence(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    run_dir = tmp_path / "run"
    snapshot = object()
    bundle = object()
    paths = object()
    evidence_runner = object()
    observed: dict[str, object] = {}
    plan = SimpleNamespace(to_payload=lambda: {"may_apply": True})

    monkeypatch.setattr(language_cli, "read_snapshot", lambda _: snapshot)

    def fake_prepare(*args: object, **kwargs: object) -> tuple[object, object]:
        observed["prepare"] = kwargs
        return bundle, paths

    monkeypatch.setattr(language_cli, "prepare_job", fake_prepare)

    def fake_gather(site: str, *, runner: object) -> tuple[str, str, str]:
        observed["gather"] = (site, runner)
        return "usage", "quota", "77: Terminated\n"

    monkeypatch.setattr(language_cli, "gather_policy_evidence", fake_gather)
    monkeypatch.setattr(language_cli, "account_active_job_count", lambda output: 0)

    def fake_evaluate(**kwargs: object) -> object:
        observed["policy"] = kwargs
        return SimpleNamespace(to_payload=lambda: {"decision": "allowed"}, may_submit=True)

    monkeypatch.setattr(language_cli, "evaluate_policy", fake_evaluate)

    def fake_submit(*args: object, **kwargs: object) -> tuple[object, None]:
        observed["submit"] = (args, kwargs)
        return plan, None

    monkeypatch.setattr(language_cli, "submit_job", fake_submit)

    language_cli.handle_grid_submit(
        run_dir,
        SHARD,
        "/remote/project",
        "/remote/source",
        "/remote/run",
        "nancy",
        2400,
        1200,
        512,
        True,
        True,
        runner=evidence_runner,
        sat_model_path=REMOTE_SAT_MODEL_PATH,
    )

    assert observed["gather"] == ("nancy", evidence_runner)
    policy = observed["policy"]
    assert isinstance(policy, dict)
    assert policy["account_job_count"] == 0
    assert policy["require_fresh_evidence"] is True
    assert policy["evidence_captured_at"] is not None
    assert policy["moment"] >= policy["evidence_captured_at"]
    submitted = observed["submit"]
    assert isinstance(submitted, tuple)
    assert submitted[1]["runner"] is evidence_runner
    assert submitted[1]["require_fresh_policy"] is True
    assert observed["prepare"]["walltime_seconds"] == 2400
    assert observed["prepare"]["remote_bundle_dir"] is None
    assert _stdout(capsys) == {
        "applied": False,
        "plan": {"may_apply": True},
        "result": None,
    }


def test_grid_status_passes_the_backend_job_name_resolver_for_ambiguous_apply(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    snapshot = object()
    bundle = SimpleNamespace(bundle_id="bundle-status")
    paths = object()
    reconciliation = SimpleNamespace(
        to_payload=lambda: {
            "state": "unknown",
            "detail": "resolved by name",
            "needs_operator_attention": True,
        }
    )
    observed: dict[str, object] = {}

    monkeypatch.setattr(language_cli, "read_snapshot", lambda _: snapshot)
    monkeypatch.setattr(language_cli, "bundle_for_shard", lambda received, shard: bundle)
    monkeypatch.setattr(language_cli, "job_paths", lambda received_run, received_bundle: paths)

    def resolver(job_name: str) -> int | None:
        observed["resolver_name"] = job_name
        return 77

    monkeypatch.setattr(language_cli, "resolve_job_name", resolver)

    def fake_reconcile(
        received_paths: object,
        *,
        apply: bool,
        resolve_job_name: object,
    ) -> object:
        observed["reconcile"] = (received_paths, apply, resolve_job_name)
        return reconciliation

    monkeypatch.setattr(language_cli, "reconcile_job", fake_reconcile)

    language_cli.handle_grid_status(Path("/run"), SHARD, True)

    assert observed["reconcile"] == (paths, True, resolver)
    assert _stdout(capsys) == {
        "bundle_id": "bundle-status",
        "detail": "resolved by name",
        "needs_operator_attention": True,
        "shard": SHARD,
        "state": "unknown",
    }


def test_grid_collect_retrieves_imports_and_acknowledges_terminal_results(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    run_dir = tmp_path / "run"
    run_dir.mkdir()
    (run_dir / "snapshot.json").write_text('{"snapshot": true}\n', encoding="utf-8")
    retrieved_run_dir = tmp_path / "retrieved"
    paths = object()
    bundle = SimpleNamespace(bundle_id="bundle-collect")
    report = SimpleNamespace(
        to_payload=lambda: {
            "complete": True,
            "annotation_count": 6,
            "issues": [],
        }
    )
    acknowledgment = SimpleNamespace(
        to_payload=lambda: {
            "terminal_state": "terminated",
            "result_acknowledged": True,
            "result_complete": True,
        }
    )
    transfer_argv = ("rsync", "--archive", "--", "/remote/run/", "/retrieved/")
    observed: list[object] = []

    monkeypatch.setattr(language_cli, "build_result_retrieval_argv", lambda *args: transfer_argv)

    def fake_runner(argv: tuple[str, ...], timeout: float) -> SimpleNamespace:
        observed.append(("transfer", argv, timeout))
        return SimpleNamespace(returncode=0, stdout="received\n", stderr="", timed_out=False)

    def fake_import(local_run: Path, incoming: Path, shard: str) -> object:
        observed.append(("import", local_run, incoming, shard))
        return report

    def fake_ack(received_paths: object, received_report: object) -> object:
        observed.append(("ack", received_paths, received_report))
        return acknowledgment

    monkeypatch.setattr(language_cli, "import_retrieved_results", fake_import, raising=False)
    monkeypatch.setattr(language_cli, "bundle_for_shard", lambda *_: bundle)
    monkeypatch.setattr(language_cli, "job_paths", lambda *_: paths)
    monkeypatch.setattr(language_cli, "read_snapshot", lambda _: object())
    monkeypatch.setattr(language_cli, "acknowledge_collected_results", fake_ack, raising=False)

    def fake_adopt(received_paths: object, incoming: Path, received_bundle: object) -> None:
        observed.append(("adopt", received_paths, incoming, received_bundle))

    monkeypatch.setattr(language_cli, "adopt_retrieved_intent", fake_adopt, raising=False)

    language_cli.handle_grid_collect(
        run_dir,
        SHARD,
        remote_bundle_dir="/remote/bundle",
        retrieved_run_dir=retrieved_run_dir,
        apply=True,
        runner=fake_runner,
    )

    assert observed == [
        ("transfer", transfer_argv, 120.0),
        ("import", run_dir, retrieved_run_dir, SHARD),
        ("adopt", paths, retrieved_run_dir, bundle),
        ("ack", paths, report),
    ]
    assert (retrieved_run_dir / "snapshot.json").read_text(encoding="utf-8") == (
        '{"snapshot": true}\n'
    )
    assert _stdout(capsys) == {
        "acknowledgment": {
            "result_acknowledged": True,
            "result_complete": True,
            "terminal_state": "terminated",
        },
        "annotation_count": 6,
        "applied": True,
        "complete": True,
        "issues": [],
        "retrieved_run_dir": str(retrieved_run_dir),
        "run_dir": str(run_dir),
        "shard": SHARD,
        "transfer_argv": list(transfer_argv),
        "transfer_result": {
            "argv": list(transfer_argv),
            "returncode": 0,
            "stderr": "",
            "stdout": "received\n",
            "timed_out": False,
        },
    }


def test_grid_collect_remote_apply_requires_the_local_snapshot(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    transfer_argv = ("rsync", "--archive", "--", "/remote/run/", "/retrieved/")
    monkeypatch.setattr(language_cli, "build_result_retrieval_argv", lambda *args: transfer_argv)

    with pytest.raises(language_cli.GridOperatorError, match="local run snapshot is missing"):
        language_cli.handle_grid_collect(
            tmp_path / "run",
            SHARD,
            remote_bundle_dir="/remote/bundle",
            retrieved_run_dir=tmp_path / "retrieved",
            apply=True,
            runner=lambda *_: pytest.fail("snapshot validation must precede transport"),
        )


@pytest.mark.parametrize("invalid_destination", ["directory", "snapshot"])
def test_grid_collect_remote_apply_rejects_symlink_staging_destinations(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    invalid_destination: str,
) -> None:
    run_dir = tmp_path / "run"
    run_dir.mkdir()
    (run_dir / "snapshot.json").write_text("{}\n", encoding="utf-8")
    retrieved_run_dir = tmp_path / "retrieved"
    if invalid_destination == "directory":
        actual = tmp_path / "actual-retrieved"
        actual.mkdir()
        retrieved_run_dir.symlink_to(actual, target_is_directory=True)
    else:
        retrieved_run_dir.mkdir()
        actual = tmp_path / "actual-snapshot.json"
        actual.write_text("{}\n", encoding="utf-8")
        (retrieved_run_dir / "snapshot.json").symlink_to(actual)
    transfer_argv = ("rsync", "--archive", "--", "/remote/run/", "/retrieved/")
    monkeypatch.setattr(language_cli, "build_result_retrieval_argv", lambda *args: transfer_argv)

    with pytest.raises(language_cli.GridOperatorError, match="must not be a symlink"):
        language_cli.handle_grid_collect(
            run_dir,
            SHARD,
            remote_bundle_dir="/remote/bundle",
            retrieved_run_dir=retrieved_run_dir,
            apply=True,
            runner=lambda *_: pytest.fail("symlink validation must precede transport"),
        )


def test_grid_collect_remote_apply_reports_snapshot_copy_failure(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    run_dir = tmp_path / "run"
    run_dir.mkdir()
    (run_dir / "snapshot.json").write_text("{}\n", encoding="utf-8")
    transfer_argv = ("rsync", "--archive", "--", "/remote/run/", "/retrieved/")
    monkeypatch.setattr(language_cli, "build_result_retrieval_argv", lambda *args: transfer_argv)

    def fail_copy(*args: object, **kwargs: object) -> None:
        raise OSError("read-only")

    monkeypatch.setattr(language_cli.shutil, "copyfile", fail_copy)

    with pytest.raises(language_cli.GridOperatorError, match="cannot seed retrieved run snapshot"):
        language_cli.handle_grid_collect(
            run_dir,
            SHARD,
            remote_bundle_dir="/remote/bundle",
            retrieved_run_dir=tmp_path / "retrieved",
            apply=True,
            runner=lambda *_: pytest.fail("copy failure must precede transport"),
        )


def test_grid_collect_remote_plan_does_not_retrieve_or_import_without_apply(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    run_dir = tmp_path / "run"
    retrieved_run_dir = tmp_path / "retrieved"
    transfer_argv = ("rsync", "--archive", "--", "/remote/run/", "/retrieved/")
    monkeypatch.setattr(language_cli, "build_result_retrieval_argv", lambda *args: transfer_argv)
    monkeypatch.setattr(
        language_cli,
        "import_retrieved_results",
        lambda *_: pytest.fail("dry collect must not import results"),
        raising=False,
    )

    language_cli.handle_grid_collect(
        run_dir,
        SHARD,
        remote_bundle_dir="/remote/bundle",
        retrieved_run_dir=retrieved_run_dir,
        apply=False,
        runner=lambda *_: pytest.fail("dry collect must not run transport"),
    )

    assert _stdout(capsys) == {
        "acknowledgment": None,
        "applied": False,
        "report": None,
        "retrieved_run_dir": str(retrieved_run_dir),
        "run_dir": str(run_dir),
        "shard": SHARD,
        "transfer_argv": list(transfer_argv),
        "transfer_result": None,
    }


@pytest.mark.parametrize(
    "account_job_output",
    [pytest.param("", id="oar-2-blank"), pytest.param("{}", id="oar-3-empty-map")],
)
def test_grid_submit_with_the_apply_gate_uses_live_policy_evidence(
    tmp_path: Path,
    project: Path,
    source: Path,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
    night_clock: datetime,
    account_job_output: str,
) -> None:
    run_dir = _prepare_run(tmp_path, project, source)
    capsys.readouterr()
    fake_bin = tmp_path / "bin"
    fake_bin.mkdir()
    for name, body in (
        (
            "usagepolicycheck",
            'echo \'{"start_time":0,"stop_time":0,"jobs":[],"total_jobs":0,"limits":{}}\'',
        ),
        (
            "quota",
            'echo "Filesystem used soft hard grace files soft hard grace"\n'
            'echo "/home/user 100 1000 2000 0 10 1000 2000 0"',
        ),
        ("oarstat", f"printf %s {account_job_output!r}"),
        ("oarsub", 'echo "OAR_JOB_ID=8123"'),
    ):
        path = fake_bin / name
        path.write_text(f"#!/usr/bin/env bash\n{body}\n", encoding="utf-8")
        path.chmod(0o700)
    monkeypatch.setenv("PATH", f"{fake_bin}{os.pathsep}{os.environ['PATH']}")

    code = run(
        [
            "language",
            "grid",
            "submit",
            "--sat-model-path",
            SAT_MODEL_PATH,
            "--run-dir",
            str(run_dir),
            "--shard",
            SHARD,
            "--site",
            "nancy",
            "--allow-daytime",
            "--apply",
            *_REMOTE,
        ]
    )
    payload = _stdout(capsys)

    assert code == 0
    assert payload["applied"] is True
    result = payload["result"]
    assert isinstance(result, dict)
    assert result["outcome"] == "submitted"
    assert result["job_id"] == 8123
    assert payload["plan"]["policy"]["decision"] == "allowed"

    assert run(["language", "grid", "status", "--run-dir", str(run_dir), "--shard", SHARD]) == 0
    status = _stdout(capsys)
    assert status["intent"]["job_id"] == 8123
