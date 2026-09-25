"""End-to-end behaviour of the ``language`` command group on synthetic data."""

import io
import json
import os
from contextlib import nullcontext
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace

import pytest
from shapely.geometry import Polygon

from osm_polygon_description_tag import language_cli
from osm_polygon_description_tag.cli import run
from osm_polygon_description_tag.dataset.languages.checkpoint import (
    exclusive_worker_lock,
    part_name_for_offset,
    shard_paths,
)
from osm_polygon_description_tag.dataset.languages.detector import LanguageDetector
from osm_polygon_description_tag.dataset.languages.models import (
    CASCADE_DETECTOR_NAME,
    DEFAULT_LANGUAGE_POLICY,
    DEFAULT_LANGUAGE_SCOPE,
    LanguagePolicy,
    cascade_model_identity,
    language_model_identity,
)
from osm_polygon_description_tag.storage import write_geoparquet
from tests.conftest import make_record_dict
from tests.helpers.messages import exactly
from tests.helpers.sentences import REMOTE_SAT_MODEL_PATH, fake_splitter

SAT_MODEL_PATH = "/models/sat-3l-sm/model.safetensors"
SHARD = "region.parquet"
# A Tuesday at 22:00 Europe/Paris: outside the blocked weekday daytime window,
# with room for the whole walltime before the next 09:00 boundary.
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


@pytest.mark.parametrize("changed", ["src/example.py", "uv.lock"])
def test_run_rejects_changed_project_artifacts_before_building_the_detector(
    tmp_path: Path,
    project: Path,
    source: Path,
    stub_detector: list[LanguagePolicy],
    capsys: pytest.CaptureFixture[str],
    changed: str,
) -> None:
    run_dir = _prepare_run(tmp_path, project, source)
    capsys.readouterr()
    (project / changed).write_text("changed after snapshot\n", encoding="utf-8")

    code = run(
        [
            "language",
            "run",
            "--sat-model-path",
            SAT_MODEL_PATH,
            "--source-root",
            str(source),
            "--run-dir",
            str(run_dir),
            "--shard",
            SHARD,
        ]
    )

    assert code == 1
    assert "fingerprint" in capsys.readouterr().err
    assert stub_detector == []
    assert not (run_dir / "shards").exists()


def test_run_constructs_the_language_scope_frozen_in_the_snapshot(
    tmp_path: Path,
    project: Path,
    source: Path,
    stub_detector: list[LanguagePolicy],
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    scope = ("eng", "fra")
    run_dir = tmp_path / "scoped-run"
    language_cli.prepare_snapshot(
        source,
        run_dir,
        code_fingerprint=language_cli.fingerprint_project_source(project),
        lock_fingerprint=language_cli.fingerprint_lockfile(project),
        model_identity=language_model_identity(LanguagePolicy(), language_scope=scope),
    )
    observed = []
    build = language_cli.build_lingua_detector

    def scoped_builder(policy: LanguagePolicy, *, language_codes=None):
        observed.append(language_codes)
        return build(policy, language_codes=language_codes)

    monkeypatch.setattr(language_cli, "build_lingua_detector", scoped_builder)

    assert (
        run(
            [
                "language",
                "run",
                "--sat-model-path",
                SAT_MODEL_PATH,
                "--source-root",
                str(source),
                "--run-dir",
                str(run_dir),
                "--shard",
                SHARD,
            ]
        )
        == 0
    )
    capsys.readouterr()
    assert observed == [scope]


def test_run_refuses_a_detector_with_a_different_configuration(
    tmp_path: Path,
    project: Path,
    source: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    run_dir = _prepare_run(tmp_path, project, source)
    capsys.readouterr()
    detector = LanguageDetector(
        _confidence_values,
        identity=language_model_identity(LanguagePolicy(), language_scope=("eng",)),
    )
    monkeypatch.setattr(language_cli, "build_lingua_detector", lambda *args, **kwargs: detector)
    monkeypatch.setattr(language_cli, "build_language_detector", lambda *args, **kwargs: detector)

    code = run(
        [
            "language",
            "run",
            "--sat-model-path",
            SAT_MODEL_PATH,
            "--source-root",
            str(source),
            "--run-dir",
            str(run_dir),
            "--shard",
            SHARD,
        ]
    )

    assert code == 1
    assert capsys.readouterr().err == (
        "error: detector configuration does not match the immutable snapshot\n"
    )
    assert not (run_dir / "shards").exists()


def _confidence_values(text: str) -> dict[str, float]:
    if text.startswith("Le "):
        return {"fra": 0.95, "eng": 0.05}
    return {"eng": 0.9, "fra": 0.1}


@pytest.fixture
def stub_detector(monkeypatch: pytest.MonkeyPatch) -> list[LanguagePolicy]:
    """Replace the real Lingua construction so the CLI loads no language model."""
    policies: list[LanguagePolicy] = []

    def _build(
        policy: LanguagePolicy,
        *,
        language_codes: tuple[str, ...] | None = None,
        glotlid_model_path: Path | None = None,
    ) -> object:
        policies.append(policy)
        identity = language_model_identity(
            policy, language_scope=language_codes or DEFAULT_LANGUAGE_SCOPE
        )
        return LanguageDetector(_confidence_values, policy=policy, identity=identity)

    def _build_cascade(
        policy: LanguagePolicy,
        *,
        language_codes: tuple[str, ...] | None = None,
        glotlid_model_path: Path | None = None,
    ) -> object:
        _build(policy, language_codes=language_codes, glotlid_model_path=glotlid_model_path)
        return LanguageDetector(
            _confidence_values,
            policy=policy,
            identity=cascade_model_identity(
                policy, language_scope=language_codes or DEFAULT_LANGUAGE_SCOPE
            ),
        )

    monkeypatch.setattr(language_cli, "build_lingua_detector", _build)
    monkeypatch.setattr(language_cli, "build_language_detector", _build_cascade)
    return policies


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


def test_prepare_freezes_a_snapshot_and_reports_it(
    tmp_path: Path, project: Path, source: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    run_dir = tmp_path / "run"

    code = run(
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
    payload = _stdout(capsys)

    assert code == 0
    assert payload["source_file_count"] == 1
    assert payload["input_row_count"] == 6
    assert payload["shards"] == [SHARD]
    assert payload["library_name"] == "lingua-language-detector"
    assert payload["library_version"] == "2.2.0"
    assert (run_dir / "snapshot.json").is_file()
    assert len(payload["snapshot_id"]) == 64


def test_prepare_is_idempotent_for_unchanged_inputs(
    tmp_path: Path, project: Path, source: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    arguments = [
        "language",
        "prepare",
        "--source-root",
        str(source),
        "--run-dir",
        str(tmp_path / "run"),
        "--project-root",
        str(project),
    ]

    assert run(arguments) == 0
    first = _stdout(capsys)
    assert run(arguments) == 0
    second = _stdout(capsys)

    assert first["snapshot_id"] == second["snapshot_id"]


def test_prepare_reports_source_drift_on_stderr(
    tmp_path: Path,
    project: Path,
    source: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    arguments = [
        "language",
        "prepare",
        "--source-root",
        str(source),
        "--run-dir",
        str(tmp_path / "run"),
        "--project-root",
        str(project),
    ]
    assert run(arguments) == 0
    capsys.readouterr()
    write_geoparquet(
        iter(
            [
                make_record_dict(
                    Polygon([(0, 0), (0, 2), (2, 2), (2, 0)]),
                    {"description": "A different description"},
                )
            ]
        ),
        source / SHARD,
        batch_size=1,
    )

    code = run(arguments)
    captured = capsys.readouterr()

    assert code == 1
    assert captured.out == ""
    assert "immutable snapshot identity differs" in captured.err


def test_a_custom_policy_changes_the_configuration_fingerprint(
    tmp_path: Path, project: Path, source: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    def _prepare(run_dir: Path, *extra: str) -> dict[str, object]:
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
                    *extra,
                ]
            )
            == 0
        )
        return _stdout(capsys)

    default = _prepare(tmp_path / "default")
    strict = _prepare(tmp_path / "strict", "--min-alphabetic-chars", "9")

    assert default["model_config_fingerprint"] != strict["model_config_fingerprint"]
    assert default["snapshot_id"] != strict["snapshot_id"]


def test_run_processes_a_shard_and_validate_reports_completeness(
    tmp_path: Path,
    project: Path,
    source: Path,
    capsys: pytest.CaptureFixture[str],
    stub_detector: list[LanguagePolicy],
) -> None:
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
    capsys.readouterr()

    code = run(
        [
            "language",
            "run",
            "--sat-model-path",
            SAT_MODEL_PATH,
            "--source-root",
            str(source),
            "--run-dir",
            str(run_dir),
            "--shard",
            SHARD,
            "--batch-size",
            "3",
        ]
    )
    processed = _stdout(capsys)

    assert code == 0
    assert processed["complete"] is True
    assert processed["status"] == "complete"
    assert processed["input_row_count"] == 6
    assert processed["annotation_count"] == 12
    assert processed["part_count"] == 2
    assert stub_detector == [LanguagePolicy()]

    assert run(["language", "validate", "--run-dir", str(run_dir)]) == 0
    report = _stdout(capsys)

    assert report["complete"] is True
    assert report["annotation_count"] == 12
    assert report["shard_count"] == 1
    assert report["issues"] == []


def test_run_resumes_after_an_exhausted_budget(
    tmp_path: Path,
    project: Path,
    source: Path,
    capsys: pytest.CaptureFixture[str],
    stub_detector: list[LanguagePolicy],
) -> None:
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
    capsys.readouterr()
    arguments = [
        "language",
        "run",
        "--sat-model-path",
        SAT_MODEL_PATH,
        "--source-root",
        str(source),
        "--run-dir",
        str(run_dir),
        "--shard",
        SHARD,
        "--batch-size",
        "3",
    ]

    assert run([*arguments, "--budget-seconds", "0.000001"]) == 0
    paused = _stdout(capsys)
    assert paused["complete"] is False
    assert paused["status"] == "paused"

    assert run(arguments) == 0
    finished = _stdout(capsys)

    assert finished["complete"] is True
    assert finished["resumed_from"] == paused["input_cursor"]
    assert finished["annotation_count"] == 12


def test_validate_reports_an_incomplete_run_without_repairing_it(
    tmp_path: Path,
    project: Path,
    source: Path,
    capsys: pytest.CaptureFixture[str],
    stub_detector: list[LanguagePolicy],
) -> None:
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
    capsys.readouterr()

    assert run(["language", "validate", "--run-dir", str(run_dir), "--shard", SHARD]) == 0
    report = _stdout(capsys)

    assert report["complete"] is False
    assert report["shards"][0]["status"] == "missing"
    assert not shard_paths(run_dir, SHARD).checkpoint.exists()


def test_run_reports_an_unknown_shard_on_stderr(
    tmp_path: Path,
    project: Path,
    source: Path,
    capsys: pytest.CaptureFixture[str],
    stub_detector: list[LanguagePolicy],
) -> None:
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
    capsys.readouterr()

    code = run(
        [
            "language",
            "run",
            "--sat-model-path",
            SAT_MODEL_PATH,
            "--source-root",
            str(source),
            "--run-dir",
            str(run_dir),
            "--shard",
            "absent.parquet",
        ]
    )
    captured = capsys.readouterr()

    assert code == 1
    assert captured.out == ""
    assert "not in snapshot" in captured.err


def test_a_second_worker_is_rejected_while_the_run_is_locked(
    tmp_path: Path,
    project: Path,
    source: Path,
    capsys: pytest.CaptureFixture[str],
    stub_detector: list[LanguagePolicy],
) -> None:
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
    capsys.readouterr()

    with exclusive_worker_lock(run_dir):
        code = run(
            [
                "language",
                "run",
                "--sat-model-path",
                SAT_MODEL_PATH,
                "--source-root",
                str(source),
                "--run-dir",
                str(run_dir),
                "--shard",
                SHARD,
            ]
        )
    captured = capsys.readouterr()

    assert code == 1
    assert captured.out == ""
    assert "already locked" in captured.err
    assert not shard_paths(run_dir, SHARD).checkpoint.exists()
    assert stub_detector == []


def test_validate_reports_a_missing_run_directory_on_stderr(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    code = run(["language", "validate", "--run-dir", str(tmp_path / "absent")])
    captured = capsys.readouterr()

    assert code == 1
    assert captured.out == ""
    assert "cannot read snapshot" in captured.err


def test_the_language_group_requires_a_subcommand(capsys: pytest.CaptureFixture[str]) -> None:
    code = run(["language"])

    assert code == 2
    assert "Usage:" in capsys.readouterr().out


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


def test_grid_prepare_writes_a_bundle_and_script(
    tmp_path: Path, project: Path, source: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    run_dir = _prepare_run(tmp_path, project, source)
    capsys.readouterr()

    code = run(
        ["language", "grid", "prepare", "--run-dir", str(run_dir), "--shard", SHARD, *_REMOTE]
    )
    payload = _stdout(capsys)

    assert code == 0
    assert payload["shard"] == SHARD
    assert payload["input_row_count"] == 6
    assert Path(str(payload["script"])).is_file()
    assert len(str(payload["bundle_id"])) == 64


def test_grid_submit_without_the_apply_gate_only_plans(
    tmp_path: Path,
    project: Path,
    source: Path,
    capsys: pytest.CaptureFixture[str],
    night_clock: datetime,
) -> None:
    run_dir = _prepare_run(tmp_path, project, source)
    capsys.readouterr()

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
            *_REMOTE,
        ]
    )
    payload = _stdout(capsys)

    assert code == 0
    assert payload["applied"] is False
    assert payload["result"] is None
    plan = payload["plan"]
    assert isinstance(plan, dict)
    assert plan["argv"][0] == "oarsub"
    # Without consulting the scheduler there is no evidence, so it fails closed.
    assert plan["policy"]["decision"] == "unknown"
    assert plan["may_apply"] is False


def test_grid_status_reports_that_nothing_was_submitted(
    tmp_path: Path, project: Path, source: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    run_dir = _prepare_run(tmp_path, project, source)
    capsys.readouterr()

    code = run(["language", "grid", "status", "--run-dir", str(run_dir), "--shard", SHARD])
    payload = _stdout(capsys)

    assert code == 0
    assert payload["intent"] is None
    assert payload["needs_operator_attention"] is False


def test_grid_collect_reports_run_completeness(
    tmp_path: Path,
    project: Path,
    source: Path,
    capsys: pytest.CaptureFixture[str],
    stub_detector: list[LanguagePolicy],
) -> None:
    run_dir = _prepare_run(tmp_path, project, source)
    capsys.readouterr()

    assert run(["language", "grid", "collect", "--run-dir", str(run_dir), "--shard", SHARD]) == 0
    assert _stdout(capsys)["complete"] is False

    assert (
        run(
            [
                "language",
                "run",
                "--sat-model-path",
                SAT_MODEL_PATH,
                "--source-root",
                str(source),
                "--run-dir",
                str(run_dir),
                "--shard",
                SHARD,
            ]
        )
        == 0
    )
    capsys.readouterr()

    assert run(["language", "grid", "collect", "--run-dir", str(run_dir), "--shard", SHARD]) == 0
    payload = _stdout(capsys)

    assert payload["complete"] is True
    assert payload["annotation_count"] == 12


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


def test_grid_stage_quarantines_and_reports_uncheckpointed_orphans(
    tmp_path: Path,
    project: Path,
    source: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    run_dir = _prepare_run(tmp_path, project, source)
    stage_argv = [
        "language",
        "grid",
        "stage",
        "--sat-model-path",
        SAT_MODEL_PATH,
        "--run-dir",
        str(run_dir),
        "--project-root",
        str(project),
        "--source-root",
        str(source),
        "--shard",
        SHARD,
        "--remote-bundle-dir",
        "/scratch/language-bundle",
        "--glotlid-model-path",
        "/home/user/models/glotlid-v3/model_v3.bin",
    ]
    assert run(stage_argv) == 0
    capsys.readouterr()
    orphan = part_name_for_offset(0)
    parts = shard_paths(run_dir, SHARD).parts
    parts.mkdir(parents=True, exist_ok=True)
    (parts / orphan).write_bytes(b"an uncheckpointed orphan part")

    assert run(stage_argv) == 0

    payload = _stdout(capsys)
    assert payload["quarantined"] == [f"parts/{orphan}"]
    assert not (parts / orphan).exists()
    assert (shard_paths(run_dir, SHARD).root / "quarantine" / "parts" / orphan).is_file()


def test_grid_stage_then_submit_reuses_the_exact_staged_script_contract(
    tmp_path: Path,
    project: Path,
    source: Path,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
    night_clock: datetime,
) -> None:
    run_dir = _prepare_run(tmp_path, project, source)
    capsys.readouterr()
    remote_bundle_dir = "/scratch/language-bundle"
    processing_seconds = 900
    batch_size = 256
    walltime_seconds = 1500
    observed: list[tuple[str, ...]] = []

    def fake_runner(argv: tuple[str, ...] | list[str], timeout: float) -> SimpleNamespace:
        received = tuple(argv)
        observed.append(received)
        if received[0] == "rsync":
            return SimpleNamespace(returncode=0, stdout="transferred\n", stderr="", timed_out=False)
        if received[0] == "usagepolicycheck":
            return SimpleNamespace(
                returncode=0,
                stdout='{"start_time":0,"stop_time":0,"jobs":[],"total_jobs":0,"limits":{}}',
                stderr="",
                timed_out=False,
            )
        if received[0] == "quota":
            return SimpleNamespace(
                returncode=0,
                stdout=(
                    "Filesystem used soft hard grace files soft hard grace\n"
                    "/home/user 100 1000 2000 0 10 1000 2000 0\n"
                ),
                stderr="",
                timed_out=False,
            )
        if received[0] == "oarstat":
            return SimpleNamespace(returncode=0, stdout="{}\n", stderr="", timed_out=False)
        if received[0] == "oarsub":
            return SimpleNamespace(
                returncode=0, stdout="OAR_JOB_ID=8123\n", stderr="", timed_out=False
            )
        raise AssertionError(f"unexpected fake command: {received}")

    monkeypatch.setattr(language_cli, "grid_command_runner", fake_runner)

    assert (
        run(
            [
                "language",
                "grid",
                "stage",
                "--sat-model-path",
                SAT_MODEL_PATH,
                "--run-dir",
                str(run_dir),
                "--project-root",
                str(project),
                "--source-root",
                str(source),
                "--shard",
                SHARD,
                "--remote-bundle-dir",
                remote_bundle_dir,
                "--glotlid-model-path",
                "/home/user/models/glotlid-v3/model_v3.bin",
                "--processing-seconds",
                str(processing_seconds),
                "--batch-size",
                str(batch_size),
                "--walltime-seconds",
                str(walltime_seconds),
                "--apply",
            ]
        )
        == 0
    )
    staged = _stdout(capsys)
    assert staged["remote_bundle_dir"] == remote_bundle_dir
    assert staged["remote_project_dir"] == f"{remote_bundle_dir}/project"
    assert staged["remote_source_dir"] == f"{remote_bundle_dir}/source"
    assert staged["remote_run_dir"] == f"{remote_bundle_dir}/run"
    assert staged["processing_seconds"] == processing_seconds
    assert staged["batch_size"] == batch_size
    assert staged["walltime_seconds"] == walltime_seconds
    printed_processing_seconds = int(staged["processing_seconds"])
    printed_batch_size = int(staged["batch_size"])
    printed_walltime_seconds = int(staged["walltime_seconds"])
    script = next((run_dir / "jobs").glob("*/job.sh"))
    original_script = script.read_bytes()
    original_config = next((run_dir / "jobs").glob("*/job-config.json")).read_bytes()

    def submit_args(
        *,
        project_dir: str,
        source_dir: str,
        run_remote_dir: str,
        requested_processing: int = printed_processing_seconds,
        requested_batch: int = printed_batch_size,
        requested_walltime: int = printed_walltime_seconds,
    ) -> list[str]:
        return [
            "language",
            "grid",
            "submit",
            "--sat-model-path",
            SAT_MODEL_PATH,
            "--run-dir",
            str(run_dir),
            "--shard",
            SHARD,
            "--glotlid-model-path",
            "/home/user/models/glotlid-v3/model_v3.bin",
            "--site",
            "nancy",
            "--remote-project-dir",
            project_dir,
            "--remote-source-dir",
            source_dir,
            "--remote-run-dir",
            run_remote_dir,
            "--batch-size",
            str(requested_batch),
            "--processing-seconds",
            str(requested_processing),
            "--walltime-seconds",
            str(requested_walltime),
        ]

    for conflicting_args in (
        submit_args(
            project_dir="/other-bundle/project",
            source_dir="/other-bundle/source",
            run_remote_dir="/other-bundle/run",
        ),
        submit_args(
            project_dir=str(staged["remote_project_dir"]),
            source_dir=str(staged["remote_source_dir"]),
            run_remote_dir=str(staged["remote_run_dir"]),
            requested_processing=printed_processing_seconds + 1,
        ),
        submit_args(
            project_dir=str(staged["remote_project_dir"]),
            source_dir=str(staged["remote_source_dir"]),
            run_remote_dir=str(staged["remote_run_dir"]),
            requested_batch=printed_batch_size + 1,
        ),
    ):
        assert run(conflicting_args) == 1
        conflict = capsys.readouterr()
        assert conflict.out == ""
        assert "immutable" in conflict.err

    assert len(observed) == 1
    assert observed[0][0] == "rsync"
    assert observed[0][-1] == f"{remote_bundle_dir}/"

    successful_submit = submit_args(
        project_dir=str(staged["remote_project_dir"]),
        source_dir=str(staged["remote_source_dir"]),
        run_remote_dir=str(staged["remote_run_dir"]),
    )
    successful_submit.extend(["--allow-daytime", "--apply"])
    assert run(successful_submit) == 0
    submitted = _stdout(capsys)

    assert submitted["applied"] is True
    result = submitted["result"]
    assert isinstance(result, dict)
    assert result["job_id"] == 8123
    assert script.read_bytes() == original_script
    assert next((run_dir / "jobs").glob("*/job-config.json")).read_bytes() == original_config
    assert b"/scratch/language-bundle/project" in script.read_bytes()
    assert observed[0][0] == "rsync"
    assert observed[0][-1] == f"{remote_bundle_dir}/"
    assert observed[-1][0] == "oarsub"


def test_grid_prepare_then_submit_reuses_the_legacy_script_contract(
    tmp_path: Path,
    project: Path,
    source: Path,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
    night_clock: datetime,
) -> None:
    run_dir = _prepare_run(tmp_path, project, source)
    capsys.readouterr()
    observed: list[tuple[str, ...]] = []

    def fake_runner(argv: tuple[str, ...] | list[str], timeout: float) -> SimpleNamespace:
        received = tuple(argv)
        observed.append(received)
        if received[0] == "usagepolicycheck":
            return SimpleNamespace(
                returncode=0,
                stdout='{"start_time":0,"stop_time":0,"jobs":[],"total_jobs":0,"limits":{}}',
                stderr="",
                timed_out=False,
            )
        if received[0] == "quota":
            return SimpleNamespace(
                returncode=0,
                stdout=(
                    "Filesystem used soft hard grace files soft hard grace\n"
                    "/home/user 100 1000 2000 0 10 1000 2000 0\n"
                ),
                stderr="",
                timed_out=False,
            )
        if received[0] == "oarstat":
            return SimpleNamespace(returncode=0, stdout="{}\n", stderr="", timed_out=False)
        if received[0] == "oarsub":
            return SimpleNamespace(
                returncode=0, stdout="OAR_JOB_ID=8124\n", stderr="", timed_out=False
            )
        raise AssertionError(f"unexpected fake command: {received}")

    monkeypatch.setattr(language_cli, "grid_command_runner", fake_runner)

    assert (
        run(
            [
                "language",
                "grid",
                "prepare",
                "--sat-model-path",
                SAT_MODEL_PATH,
                "--run-dir",
                str(run_dir),
                "--shard",
                SHARD,
                *_REMOTE,
            ]
        )
        == 0
    )
    prepared = _stdout(capsys)
    script = Path(str(prepared["script"]))
    original_script = script.read_bytes()
    original_config = script.with_name("job-config.json").read_bytes()

    assert (
        run(
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
        == 0
    )
    submitted = _stdout(capsys)

    assert submitted["applied"] is True
    result = submitted["result"]
    assert isinstance(result, dict)
    assert result["job_id"] == 8124
    assert script.read_bytes() == original_script
    assert script.with_name("job-config.json").read_bytes() == original_config
    assert observed[-1][0] == "oarsub"


class _StubHub:
    """A faked Hub for CLI tests; it never contacts the network."""

    def __init__(self) -> None:
        self.uploads: list[object] = []
        self.stored: dict[str, object] = {}

    def repo_revision(self, repo_id: str) -> str:
        return "rev-1"

    def upload(self, plan: object, *, parent_revision: str | None = None) -> None:
        from osm_polygon_description_tag.publication.language_upload import RemoteFile

        self.uploads.append(plan)
        for item in plan.files:  # type: ignore[attr-defined]
            self.stored[item.relative_path] = RemoteFile(
                item.relative_path, item.size_bytes, item.sha256
            )

    def paths_info(self, repo_id: str, revision: str, paths: object) -> tuple[object, ...]:
        return tuple(self.stored[path] for path in paths if path in self.stored)  # type: ignore[union-attr]

    def dataset_configs(self, repo_id: str, revision: str) -> tuple[str, ...]:
        return ("default", "language-v1")


def _completed_run(tmp_path: Path, project: Path, source: Path) -> Path:
    run_dir = _prepare_run(tmp_path, project, source)
    assert (
        run(
            [
                "language",
                "run",
                "--sat-model-path",
                SAT_MODEL_PATH,
                "--source-root",
                str(source),
                "--run-dir",
                str(run_dir),
                "--shard",
                SHARD,
            ]
        )
        == 0
    )
    return run_dir


def test_export_writes_the_additive_tree_and_card_section(
    tmp_path: Path,
    project: Path,
    source: Path,
    capsys: pytest.CaptureFixture[str],
    stub_detector: list[LanguagePolicy],
) -> None:
    run_dir = _completed_run(tmp_path, project, source)
    capsys.readouterr()
    export_dir = tmp_path / "export"
    card = tmp_path / "card.md"

    code = run(
        [
            "language",
            "export",
            "--run-dir",
            str(run_dir),
            "--export-dir",
            str(export_dir),
            "--card-section",
            str(card),
        ]
    )
    payload = _stdout(capsys)

    assert code == 0
    assert payload["config_name"] == "language-v1"
    assert payload["stats"]["annotation_count"] == 12
    assert payload["files"] == ["language-v1/data/region.parquet"]
    assert "config_name: language-v1" in str(payload["config_yaml"])
    assert (export_dir / "language-v1/data/region.parquet").is_file()
    assert "No accuracy has been measured" in card.read_text(encoding="utf-8")


def test_export_refuses_an_incomplete_run_on_stderr(
    tmp_path: Path, project: Path, source: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    run_dir = _prepare_run(tmp_path, project, source)
    capsys.readouterr()

    code = run(
        [
            "language",
            "export",
            "--run-dir",
            str(run_dir),
            "--export-dir",
            str(tmp_path / "export"),
        ]
    )
    captured = capsys.readouterr()

    assert code == 1
    assert captured.out == ""
    assert "run is not publishable" in captured.err


def _export(
    tmp_path: Path, project: Path, source: Path, capsys: pytest.CaptureFixture[str]
) -> Path:
    run_dir = _completed_run(tmp_path, project, source)
    export_dir = tmp_path / "export"
    assert (
        run(
            [
                "language",
                "export",
                "--run-dir",
                str(run_dir),
                "--export-dir",
                str(export_dir),
            ]
        )
        == 0
    )
    capsys.readouterr()
    return export_dir


def test_publish_without_the_apply_gate_uploads_nothing(
    tmp_path: Path,
    project: Path,
    source: Path,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
    stub_detector: list[LanguagePolicy],
) -> None:
    export_dir = _export(tmp_path, project, source, capsys)
    hub = _StubHub()
    monkeypatch.setattr(language_cli, "build_language_hub", lambda **kwargs: hub)

    code = run(
        [
            "language",
            "publish",
            "--export-dir",
            str(export_dir),
            "--repo",
            "NoeFlandre/osm-polygon-description-tag",
            "--confirm-repo",
            "NoeFlandre/osm-polygon-description-tag",
        ]
    )
    payload = _stdout(capsys)

    assert code == 0
    assert payload["status"] == "planned"
    assert payload["planned_files"] == [
        "language-v1/data/region.parquet",
        "language-v1/export-manifest.json",
        "language-v1/stats.json",
    ]
    assert hub.uploads == []


def test_publish_requires_a_matching_repository_confirmation(
    tmp_path: Path,
    project: Path,
    source: Path,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
    stub_detector: list[LanguagePolicy],
) -> None:
    export_dir = _export(tmp_path, project, source, capsys)
    hub = _StubHub()
    monkeypatch.setattr(language_cli, "build_language_hub", lambda **kwargs: hub)

    code = run(
        [
            "language",
            "publish",
            "--export-dir",
            str(export_dir),
            "--repo",
            "NoeFlandre/osm-polygon-description-tag",
            "--confirm-repo",
            "someone/else",
        ]
    )
    captured = capsys.readouterr()

    assert code == 1
    assert "does not match" in captured.err
    assert hub.uploads == []


def test_publish_requires_a_baseline_revision_before_applying(
    tmp_path: Path,
    project: Path,
    source: Path,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
    stub_detector: list[LanguagePolicy],
) -> None:
    export_dir = _export(tmp_path, project, source, capsys)
    hub = _StubHub()
    monkeypatch.setattr(language_cli, "build_language_hub", lambda **kwargs: hub)

    code = run(
        [
            "language",
            "publish",
            "--export-dir",
            str(export_dir),
            "--repo",
            "NoeFlandre/osm-polygon-description-tag",
            "--confirm-repo",
            "NoeFlandre/osm-polygon-description-tag",
            "--apply",
        ]
    )
    captured = capsys.readouterr()

    assert code == 1
    assert "requires the baseline revision" in captured.err
    assert hub.uploads == []


def test_publish_with_the_apply_gate_uploads_and_verifies(
    tmp_path: Path,
    project: Path,
    source: Path,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
    stub_detector: list[LanguagePolicy],
) -> None:
    export_dir = _export(tmp_path, project, source, capsys)
    hub = _StubHub()
    monkeypatch.setattr(language_cli, "build_language_hub", lambda **kwargs: hub)
    arguments = [
        "language",
        "publish",
        "--export-dir",
        str(export_dir),
        "--repo",
        "NoeFlandre/osm-polygon-description-tag",
        "--confirm-repo",
        "NoeFlandre/osm-polygon-description-tag",
        "--baseline-revision",
        "rev-1",
        "--apply",
    ]

    assert run(arguments) == 0
    payload = _stdout(capsys)

    assert payload["status"] == "verified"
    assert payload["revision"] == "rev-1"
    assert len(hub.uploads) == 1

    assert run(arguments) == 0
    assert _stdout(capsys)["status"] == "verified"
    assert len(hub.uploads) == 1


def test_publish_refuses_a_repository_that_moved(
    tmp_path: Path,
    project: Path,
    source: Path,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
    stub_detector: list[LanguagePolicy],
) -> None:
    export_dir = _export(tmp_path, project, source, capsys)
    hub = _StubHub()
    monkeypatch.setattr(language_cli, "build_language_hub", lambda **kwargs: hub)

    code = run(
        [
            "language",
            "publish",
            "--export-dir",
            str(export_dir),
            "--repo",
            "NoeFlandre/osm-polygon-description-tag",
            "--confirm-repo",
            "NoeFlandre/osm-polygon-description-tag",
            "--baseline-revision",
            "rev-0",
            "--apply",
        ]
    )
    payload = _stdout(capsys)

    assert code == 0
    assert payload["status"] == "drifted"
    assert hub.uploads == []


# Exact option forwarding and emitted metadata of each language command.


def _payload(capsys: pytest.CaptureFixture[str]) -> dict[str, object]:
    captured = capsys.readouterr()
    assert captured.err == ""
    return json.loads(captured.out)


def test_prepare_command_forwards_all_language_policy_options(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    received: dict[str, object] = {}

    def fake_handle_prepare(
        source_root: Path,
        run_dir: Path,
        project_root: Path,
        policy: LanguagePolicy,
        *,
        model_identity: object,
    ) -> None:
        received.update(
            source_root=source_root,
            run_dir=run_dir,
            project_root=project_root,
            policy=policy,
        )

    monkeypatch.setattr(language_cli, "handle_prepare", fake_handle_prepare)

    language_cli.prepare_command(
        Path("/source"),
        Path("/run"),
        Path("/project"),
        17,
    )

    assert received == {
        "source_root": Path("/source"),
        "run_dir": Path("/run"),
        "project_root": Path("/project"),
        "policy": LanguagePolicy(min_alphabetic_chars=17),
    }


def test_prepare_command_selects_the_named_v1_policy(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    received: dict[str, object] = {}

    def fake_handle_prepare(
        source_root: Path,
        run_dir: Path,
        project_root: Path,
        policy: LanguagePolicy,
        *,
        model_identity: object,
    ) -> None:
        received["policy"] = policy

    monkeypatch.setattr(language_cli, "handle_prepare", fake_handle_prepare)

    language_cli.prepare_command(
        Path("/source"),
        Path("/run"),
        Path("/project"),
        policy_version="v1",
    )

    assert received["policy"] == DEFAULT_LANGUAGE_POLICY


def test_prepare_policy_rejects_an_unknown_named_policy() -> None:
    with pytest.raises(ValueError, match=exactly("policy_version must be 'v1'")):
        language_cli._policy(None, policy_version="v2")


def test_handle_prepare_emits_complete_snapshot_metadata(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    source_root = Path("/source")
    run_dir = Path("/run")
    project_root = Path("/project")
    policy = LanguagePolicy(min_alphabetic_chars=9)
    snapshot = SimpleNamespace(
        snapshot_id="snapshot-123",
        source_files=(
            SimpleNamespace(relative_path="north.parquet", row_count=4),
            SimpleNamespace(relative_path="south.parquet", row_count=6),
        ),
        model_config_fingerprint="config-456",
        model_identity=SimpleNamespace(
            detector_name="fake-detector", library_name="fake-detector", library_version="1.2.3"
        ),
    )
    received: dict[str, object] = {}

    def fake_prepare_snapshot(
        received_source_root: Path,
        received_run_dir: Path,
        *,
        code_fingerprint: str,
        lock_fingerprint: str,
        model_identity: object,
        policy: LanguagePolicy,
    ) -> object:
        received.update(
            source_root=received_source_root,
            run_dir=received_run_dir,
            code_fingerprint=code_fingerprint,
            lock_fingerprint=lock_fingerprint,
            model_identity=model_identity,
            policy=policy,
        )
        return snapshot

    monkeypatch.setattr(language_cli, "fingerprint_project_source", lambda _: "code-hash")
    monkeypatch.setattr(language_cli, "fingerprint_lockfile", lambda _: "lock-hash")
    monkeypatch.setattr(language_cli, "prepare_snapshot", fake_prepare_snapshot)

    result = language_cli.handle_prepare(source_root, run_dir, project_root, policy)

    assert result is snapshot
    assert received == {
        "source_root": source_root,
        "run_dir": run_dir,
        "code_fingerprint": "code-hash",
        "lock_fingerprint": "lock-hash",
        "model_identity": cascade_model_identity(policy),
        "policy": policy,
    }
    assert _payload(capsys) == {
        "detector_name": "fake-detector",
        "input_row_count": 10,
        "library_name": "fake-detector",
        "library_version": "1.2.3",
        "model_config_fingerprint": "config-456",
        "run_dir": "/run",
        "shards": ["north.parquet", "south.parquet"],
        "snapshot_id": "snapshot-123",
        "source_file_count": 2,
        "source_root": "/source",
    }


def test_handle_run_forwards_operator_configuration_and_emits_counts(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    source_root = Path("/source")
    run_dir = Path("/run")
    shard = "north.parquet"
    policy = LanguagePolicy(min_alphabetic_chars=8)
    identity = language_model_identity(policy)
    snapshot = SimpleNamespace(
        snapshot_id="snapshot-123",
        model_identity=identity,
        model_config_fingerprint=identity.config_fingerprint,
    )
    outcome = SimpleNamespace(
        shard=shard,
        status="complete",
        is_complete=True,
        resumed_from=4,
        input_cursor=10,
        input_row_count=10,
        annotation_count=15,
        completed_parts=("part-1.parquet", "part-2.parquet"),
    )
    received: dict[str, object] = {}
    seen_detector: LanguageDetector | None = None

    class FakeBudget:
        def __init__(self, seconds: float) -> None:
            self.seconds = seconds

    def fake_build(
        received_policy: LanguagePolicy,
        *,
        language_codes: tuple[str, ...] | None = None,
    ) -> LanguageDetector:
        received["detector_policy"] = received_policy
        received["language_codes"] = language_codes
        return LanguageDetector(lambda _: {"eng": 1.0}, policy=received_policy, identity=identity)

    def fake_process(
        received_run_dir: Path,
        received_source_root: Path,
        received_shard: str,
        *,
        detector: LanguageDetector,
        splitter: object,
        snapshot: object,
        batch_size: int,
        budget: FakeBudget,
    ) -> object:
        nonlocal seen_detector
        seen_detector = detector
        received["process"] = {
            "run_dir": received_run_dir,
            "source_root": received_source_root,
            "shard": received_shard,
            "detector": detector,
            "splitter": splitter,
            "snapshot": snapshot,
            "batch_size": batch_size,
            "budget_seconds": budget.seconds,
        }
        return outcome

    monkeypatch.setattr(language_cli, "read_snapshot", lambda _: snapshot)
    monkeypatch.setattr(language_cli, "verify_project_identity", lambda *_: None, raising=False)
    monkeypatch.setattr(language_cli, "build_lingua_detector", fake_build)
    monkeypatch.setattr(language_cli, "ProcessingBudget", FakeBudget)
    monkeypatch.setattr(language_cli, "exclusive_worker_lock", lambda _: nullcontext())
    monkeypatch.setattr(language_cli, "process_shard", fake_process)
    sat_splitter = fake_splitter()
    sat_dirs: list[object] = []

    def fake_build_splitter(*, model_dir: object) -> object:
        sat_dirs.append(model_dir)
        return sat_splitter

    monkeypatch.setattr(language_cli, "build_sat_splitter", fake_build_splitter)

    language_cli.handle_run(
        source_root, run_dir, shard, 3, 12.5, sat_model_path=Path("/models/sat.bin")
    )

    assert sat_dirs == [Path("/models/sat.bin")]
    assert received["detector_policy"] == policy
    assert received["language_codes"] is None
    assert seen_detector is not None
    assert isinstance(received["process"], dict)
    assert received["process"] == {
        "run_dir": run_dir,
        "source_root": source_root,
        "shard": shard,
        "detector": seen_detector,
        "splitter": sat_splitter,
        "snapshot": snapshot,
        "batch_size": 3,
        "budget_seconds": 12.5,
    }
    assert _payload(capsys) == {
        "annotation_count": 15,
        "complete": True,
        "input_cursor": 10,
        "input_row_count": 10,
        "part_count": 2,
        "resumed_from": 4,
        "shard": "north.parquet",
        "snapshot_id": "snapshot-123",
        "status": "complete",
    }


def test_handle_validate_forwards_shard_filter_and_emits_report_metadata(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    run_dir = Path("/run")
    shard = "north.parquet"
    received: dict[str, object] = {}
    report_payload = {
        "complete": False,
        "issues": ["checkpoint is missing"],
        "shards": [{"shard": shard, "status": "missing"}],
    }

    class FakeReport:
        def to_payload(self) -> dict[str, object]:
            return report_payload

    def fake_validate(
        received_run_dir: Path,
        *,
        shards: tuple[str, ...] | None,
    ) -> FakeReport:
        received.update(run_dir=received_run_dir, shards=shards)
        return FakeReport()

    monkeypatch.setattr(language_cli, "validate_run", fake_validate)

    language_cli.handle_validate(run_dir, shard)

    assert received == {"run_dir": run_dir, "shards": (shard,)}
    assert _payload(capsys) == {
        "complete": False,
        "issues": ["checkpoint is missing"],
        "run_dir": "/run",
        "shards": [{"shard": "north.parquet", "status": "missing"}],
    }


def test_handle_export_writes_nested_card_and_emits_export_metadata(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    run_dir = tmp_path / "run"
    export_dir = tmp_path / "export"
    card_section = tmp_path / "card" / "nested" / "section.md"
    export_payload = {
        "config_name": "language-v1",
        "snapshot_id": "snapshot-123",
        "model_config_fingerprint": "config-456",
        "library_name": "fake-detector",
        "library_version": "1.2.3",
        "files": ["language-v1/data/north.parquet"],
        "stats": {"annotation_count": 7, "detected_count": 5},
    }
    received: dict[str, object] = {}

    class FakeExport:
        def to_payload(self) -> dict[str, object]:
            return export_payload

    export = FakeExport()

    def fake_export_annotations(
        received_run_dir: Path,
        received_export_dir: Path,
    ) -> FakeExport:
        received.update(run_dir=received_run_dir, export_dir=received_export_dir)
        return export

    def fake_render(received_export: FakeExport) -> str:
        received["card_export"] = received_export
        return "## résumé — 日本語\n"

    monkeypatch.setattr(language_cli, "export_language_annotations", fake_export_annotations)
    monkeypatch.setattr(language_cli, "render_language_card_section", fake_render)
    monkeypatch.setattr(language_cli, "language_config_yaml", lambda: "config: language-v1\n")

    language_cli.handle_export(run_dir, export_dir, card_section)

    assert received == {
        "run_dir": run_dir,
        "export_dir": export_dir,
        "card_export": export,
    }
    assert card_section.read_bytes() == "## résumé — 日本語\n".encode()
    assert _payload(capsys) == {
        "card_section": str(card_section),
        "config_name": "language-v1",
        "config_yaml": "config: language-v1\n",
        "files": ["language-v1/data/north.parquet"],
        "library_name": "fake-detector",
        "library_version": "1.2.3",
        "model_config_fingerprint": "config-456",
        "snapshot_id": "snapshot-123",
        "stats": {"annotation_count": 7, "detected_count": 5},
        "export_dir": str(export_dir),
    }


def test_handle_export_writes_card_section_with_explicit_utf8(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    run_dir = tmp_path / "run"
    export_dir = tmp_path / "export"
    card_section = tmp_path / "card.md"

    class FakeExport:
        def to_payload(self) -> dict[str, object]:
            return {}

    real_text_encoding = io.text_encoding

    def locale_encoding_is_ascii(
        encoding: str | None,
        stacklevel: int = 2,
    ) -> str:
        if encoding is None:
            return "ascii"
        return real_text_encoding(encoding, stacklevel)

    monkeypatch.setattr(language_cli, "export_language_annotations", lambda *_: FakeExport())
    monkeypatch.setattr(language_cli, "render_language_card_section", lambda _: "café\n")
    monkeypatch.setattr(language_cli, "language_config_yaml", lambda: "")
    monkeypatch.setattr(io, "text_encoding", locale_encoding_is_ascii)

    language_cli.handle_export(run_dir, export_dir, card_section)

    assert card_section.read_bytes() == "café\n".encode()
    assert _payload(capsys) == {
        "card_section": str(card_section),
        "config_yaml": "",
        "export_dir": str(export_dir),
    }


def test_handle_publish_forwards_baseline_and_emits_plan_metadata(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    export_dir = tmp_path / "export"
    repo = "owner/repository"
    export = object()
    plan = SimpleNamespace(
        identity_sha256="plan-789",
        files=(
            SimpleNamespace(relative_path="language-v1/data/north.parquet"),
            SimpleNamespace(relative_path="language-v1/stats.json"),
        ),
    )
    outcome = SimpleNamespace(
        to_payload=lambda: {
            "state_schema_version": 1,
            "status": "planned",
            "repo_id": repo,
            "plan_identity": "plan-789",
            "revision": "hub-rev",
            "verified_files": [],
            "issues": [],
        }
    )
    calls: dict[str, object] = {}

    class FakeHub:
        def repo_revision(self, received_repo: str) -> str:
            calls["revision_repo"] = received_repo
            return "hub-rev"

    hub = FakeHub()

    def fake_read(received_export_dir: Path) -> object:
        calls["read_export_dir"] = received_export_dir
        return export

    def fake_plan(
        received_export: object,
        received_repo: str,
        *,
        confirm_repo: str,
    ) -> object:
        calls["plan"] = (received_export, received_repo, confirm_repo)
        return plan

    def fake_publish(
        received_plan: object,
        received_hub: FakeHub,
        *,
        baseline_revision: str,
        apply: bool,
        state_path: Path,
    ) -> object:
        calls["publish"] = {
            "plan": received_plan,
            "hub": received_hub,
            "baseline_revision": baseline_revision,
            "apply": apply,
            "state_path": state_path,
        }
        return outcome

    monkeypatch.setattr(language_cli, "read_language_export", fake_read)
    monkeypatch.setattr(language_cli, "build_language_upload_plan", fake_plan)
    monkeypatch.setattr(language_cli, "build_language_hub", lambda: hub)
    monkeypatch.setattr(language_cli, "publish_language_export", fake_publish)

    language_cli.handle_publish(export_dir, repo, repo, None, False)

    assert calls["read_export_dir"] == export_dir
    assert calls["plan"] == (export, repo, repo)
    assert calls["revision_repo"] == repo
    assert calls["publish"] == {
        "plan": plan,
        "hub": hub,
        "baseline_revision": "hub-rev",
        "apply": False,
        "state_path": export_dir / "language-publication.json",
    }
    assert _payload(capsys) == {
        "baseline_revision": "hub-rev",
        "issues": [],
        "plan_identity": "plan-789",
        "plan_identity_sha256": "plan-789",
        "planned_files": [
            "language-v1/data/north.parquet",
            "language-v1/stats.json",
        ],
        "repo_id": repo,
        "revision": "hub-rev",
        "state_schema_version": 1,
        "status": "planned",
        "verified_files": [],
    }


def test_handle_publish_rejects_apply_without_an_exact_baseline_error(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    export_dir = tmp_path / "export"
    repo = "owner/repository"
    export = object()
    plan = SimpleNamespace(files=())
    calls: list[str] = []

    class FakeHub:
        pass

    def fake_publish(*_: object, **__: object) -> object:
        calls.append("publish")
        raise AssertionError("publishing must not start before baseline validation")

    monkeypatch.setattr(language_cli, "read_language_export", lambda _: export)
    monkeypatch.setattr(
        language_cli,
        "build_language_upload_plan",
        lambda received_export, received_repo, *, confirm_repo: plan,
    )
    monkeypatch.setattr(language_cli, "build_language_hub", FakeHub)
    monkeypatch.setattr(language_cli, "publish_language_export", fake_publish)

    with pytest.raises(language_cli.LanguagePublicationError) as error:
        language_cli.handle_publish(export_dir, repo, repo, None, True)

    assert str(error.value) == (
        "applying a publication requires the baseline revision the plan was built against"
    )
    assert calls == []


def test_handle_prepare_defaults_to_the_cascade_identity(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    source_root = Path("/source")
    run_dir = Path("/run")
    project_root = Path("/project")
    policy = LanguagePolicy()
    identity = cascade_model_identity(policy)
    snapshot = SimpleNamespace(
        snapshot_id="snapshot-123",
        source_files=(SimpleNamespace(relative_path="north.parquet", row_count=4),),
        model_config_fingerprint=identity.config_fingerprint,
        model_identity=identity,
    )
    received: dict[str, object] = {}

    def fake_prepare_snapshot(
        received_source_root: Path,
        received_run_dir: Path,
        *,
        code_fingerprint: str,
        lock_fingerprint: str,
        model_identity: object,
        policy: LanguagePolicy,
    ) -> object:
        received.update(
            source_root=received_source_root,
            run_dir=received_run_dir,
            code_fingerprint=code_fingerprint,
            lock_fingerprint=lock_fingerprint,
            model_identity=model_identity,
            policy=policy,
        )
        return snapshot

    monkeypatch.setattr(language_cli, "fingerprint_project_source", lambda _: "code-hash")
    monkeypatch.setattr(language_cli, "fingerprint_lockfile", lambda _: "lock-hash")
    monkeypatch.setattr(language_cli, "prepare_snapshot", fake_prepare_snapshot)

    assert language_cli.handle_prepare(source_root, run_dir, project_root, policy) is snapshot
    assert received["model_identity"] == identity
    assert _payload(capsys)["detector_name"] == CASCADE_DETECTOR_NAME


def test_handle_run_uses_the_cascade_builder_and_forwards_the_glotlid_path(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    source_root = Path("/source")
    run_dir = Path("/run")
    shard = "north.parquet"
    policy = LanguagePolicy()
    identity = cascade_model_identity(policy)
    snapshot = SimpleNamespace(
        snapshot_id="snapshot-123",
        model_identity=identity,
        model_config_fingerprint=identity.config_fingerprint,
    )
    detector = LanguageDetector(lambda _: {"eng": 1.0}, policy=policy, identity=identity)
    received: dict[str, object] = {}
    outcome = SimpleNamespace(
        shard=shard,
        status="complete",
        is_complete=True,
        resumed_from=0,
        input_cursor=4,
        input_row_count=4,
        annotation_count=4,
        completed_parts=("part-1.parquet",),
    )

    def fake_build(
        received_policy: LanguagePolicy,
        *,
        language_codes: tuple[str, ...] | None = None,
        glotlid_model_path: Path | None = None,
    ) -> LanguageDetector:
        received.update(
            policy=received_policy,
            language_codes=language_codes,
            glotlid_model_path=glotlid_model_path,
        )
        return detector

    def fake_process(*args: object, **kwargs: object) -> object:
        return outcome

    monkeypatch.setattr(language_cli, "read_snapshot", lambda _: snapshot)
    monkeypatch.setattr(language_cli, "verify_project_identity", lambda *_: None)
    monkeypatch.setattr(language_cli, "build_language_detector", fake_build)
    monkeypatch.setattr(language_cli, "exclusive_worker_lock", lambda _: nullcontext())
    monkeypatch.setattr(language_cli, "process_shard", fake_process)
    monkeypatch.setattr(language_cli, "build_sat_splitter", lambda *, model_dir: fake_splitter())

    model_path = Path("/models/model_v3.bin")
    language_cli.handle_run(
        source_root,
        run_dir,
        shard,
        3,
        12.5,
        sat_model_path=Path("/models/sat.bin"),
        glotlid_model_path=model_path,
    )

    assert received == {
        "policy": policy,
        "language_codes": None,
        "glotlid_model_path": model_path,
    }
    assert _payload(capsys)["status"] == "complete"
