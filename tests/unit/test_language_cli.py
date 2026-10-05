"""End-to-end behaviour of the ``language`` command group on synthetic data."""

from datetime import datetime
from pathlib import Path

import pytest
from shapely.geometry import Polygon

from osm_polygon_description_tag import language_workflow
from osm_polygon_description_tag.cli import run
from osm_polygon_description_tag.dataset.languages.checkpoint import (
    exclusive_worker_lock,
    shard_paths,
)
from osm_polygon_description_tag.dataset.languages.detector import LanguageDetector
from osm_polygon_description_tag.dataset.languages.models import (
    LanguagePolicy,
    language_model_identity,
)
from osm_polygon_description_tag.dataset.storage import write_geoparquet
from tests.conftest import make_record_dict
from tests.helpers.language_cli import (
    SAT_MODEL_PATH,
    SHARD,
    _confidence_values,
    _prepare_run,
    _stdout,
)
from tests.helpers.language_cli import (
    _fake_sentence_splitter as _language_cli_splitter,  # noqa: F401
)
from tests.helpers.language_cli import (
    night_clock as _language_cli_night_clock,  # noqa: F401
)
from tests.helpers.language_cli import (
    project as _language_cli_project,  # noqa: F401
)
from tests.helpers.language_cli import (
    source as _language_cli_source,  # noqa: F401
)
from tests.helpers.language_cli import (
    stub_detector as _language_cli_stub_detector,  # noqa: F401
)


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
    language_workflow.prepare_snapshot(
        source,
        run_dir,
        code_fingerprint=language_workflow.fingerprint_project_source(project),
        lock_fingerprint=language_workflow.fingerprint_lockfile(project),
        model_identity=language_model_identity(LanguagePolicy(), language_scope=scope),
    )
    observed = []
    build = language_workflow.build_lingua_detector

    def scoped_builder(policy: LanguagePolicy, *, language_codes=None):
        observed.append(language_codes)
        return build(policy, language_codes=language_codes)

    monkeypatch.setattr(language_workflow, "build_lingua_detector", scoped_builder)

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
    monkeypatch.setattr(
        language_workflow, "build_lingua_detector", lambda *args, **kwargs: detector
    )
    monkeypatch.setattr(
        language_workflow, "build_language_detector", lambda *args, **kwargs: detector
    )

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
