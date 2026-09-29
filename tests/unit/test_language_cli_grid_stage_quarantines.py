"""End-to-end behaviour of the ``language`` command group on synthetic data."""

import json
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace

import pytest
from shapely.geometry import Polygon

from osm_polygon_description_tag import language_cli
from osm_polygon_description_tag.cli import run
from osm_polygon_description_tag.dataset.languages.checkpoint import (
    part_name_for_offset,
    shard_paths,
)
from osm_polygon_description_tag.dataset.languages.detector import LanguageDetector
from osm_polygon_description_tag.dataset.languages.models import (
    DEFAULT_LANGUAGE_SCOPE,
    LanguagePolicy,
    cascade_model_identity,
    language_model_identity,
)
from osm_polygon_description_tag.dataset.storage import write_geoparquet
from tests.conftest import make_record_dict
from tests.helpers.sentences import fake_splitter

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


def test_grid_stage_then_submit_reuses_the_exact_staged_script_contract(  # noqa: PLR0915 - long test; TODO(#62) split it
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

    assert code == 5  # publication failure
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

    assert code == 5  # publication failure
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

    assert code == 5  # publication failure
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
