"""Direct contracts for small public-boundary helpers.

These tests deliberately exercise helpers that are otherwise reached only by
CLI error paths or optional integrations.  Keeping those boundaries explicit
gives mutation testing a meaningful oracle without touching real data or the
Hub.
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from types import SimpleNamespace

import pytest
from click import Command, Context
from typer._click.exceptions import ClickException, UsageError

import osm_polygon_description_tag.cli as cli
import osm_polygon_description_tag.workflow.orchestrator as orchestrator
from osm_polygon_description_tag.dataset.manifest import _empty_policy_hash
from osm_polygon_description_tag.publication.models import PublishRetry


def _cli_args(tmp_path: Path, **values: object) -> SimpleNamespace:
    source_root = tmp_path / "raw"
    data_root = tmp_path / "generated"
    source_root.mkdir(exist_ok=True)
    data_root.mkdir(exist_ok=True)
    return SimpleNamespace(
        source_root=source_root,
        data_root=data_root,
        osmium="fake-osmium",
        **values,
    )


def test_cli_migration_handler_reports_migrated_files(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    args = _cli_args(tmp_path)
    monkeypatch.setattr(
        cli,
        "migrate_dataset_schema",
        lambda root: ["data/a.parquet"]
        if root == args.data_root
        else pytest.fail("wrong data root"),
    )

    assert cli.handle_migrate_schema(args) == 0
    assert json.loads(capsys.readouterr().out) == {
        "data_root": str(args.data_root),
        "migrated_files": ["data/a.parquet"],
    }


def test_cli_resolve_paths_uses_supplied_roots(tmp_path: Path) -> None:
    args = _cli_args(tmp_path)
    paths = cli._resolve_paths(args)

    assert paths.source_root == args.source_root
    assert paths.data_root == args.data_root


def test_cli_print_json_is_sorted_and_indented(capsys: pytest.CaptureFixture[str]) -> None:
    cli._print_json({"z": 1, "a": {"value": "café", "é": True}})

    assert capsys.readouterr().out == (
        '{\n  "a": {\n    "value": "café",\n    "é": true\n  },\n  "z": 1\n}\n'
    )


def test_cli_invoke_calls_handler_and_translates_keyboard_interrupt() -> None:
    args = SimpleNamespace()
    calls: list[SimpleNamespace] = []

    def handler(value: SimpleNamespace) -> None:
        calls.append(value)

    cli._invoke(handler, args)
    assert calls == [args]

    with pytest.raises(cli._Interrupted):
        cli._invoke(lambda _value: (_ for _ in ()).throw(KeyboardInterrupt), args)


def test_cli_normalize_columns_replaces_invalid_values(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("COLUMNS", "not-a-number")
    cli._normalize_columns()
    assert cli.os.environ["COLUMNS"] == "80"

    monkeypatch.setenv("COLUMNS", "120")
    cli._normalize_columns()
    assert cli.os.environ["COLUMNS"] == "120"

    monkeypatch.setenv("COLUMNS", "80")
    cli._normalize_columns()
    assert cli.os.environ["COLUMNS"] == "80"

    monkeypatch.setenv("COLUMNS", "79")
    cli._normalize_columns()
    assert cli.os.environ["COLUMNS"] == "80"

    writes: list[tuple[str, str]] = []

    class _Environment(dict[str, str]):
        def __setitem__(self, key: str, value: str) -> None:
            writes.append((key, value))
            super().__setitem__(key, value)

    environment = _Environment(COLUMNS="80")
    mutant_under_test = cli.os.environ.get("MUTANT_UNDER_TEST")
    if mutant_under_test is not None:
        dict.__setitem__(environment, "MUTANT_UNDER_TEST", mutant_under_test)
    monkeypatch.setattr(cli.os, "environ", environment)
    cli._normalize_columns()
    assert writes == []


def test_cli_invoke_app_forwards_arguments(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[dict[str, object]] = []

    def fake_app(**kwargs: object) -> None:
        calls.append(kwargs)

    monkeypatch.setattr(cli, "app", fake_app)
    assert cli._invoke_app(["inspect", "--help"]) == 0
    assert calls == [
        {
            "args": ["inspect", "--help"],
            "prog_name": "osm-polygon-description-tag",
            "standalone_mode": False,
        }
    ]


def test_cli_configure_terminal_sets_stable_noninteractive_defaults(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class _Stdout:
        def isatty(self) -> bool:
            return False

    monkeypatch.setattr(cli.sys, "stdout", _Stdout())
    monkeypatch.setattr(cli.rich_utils, "MAX_WIDTH", None)
    monkeypatch.setattr(cli.rich_utils, "FORCE_TERMINAL", True)
    monkeypatch.setenv("COLUMNS", "120")

    cli._configure_terminal()

    assert cli.rich_utils.MAX_WIDTH == 80
    assert cli.rich_utils.FORCE_TERMINAL is False
    assert cli.os.environ["COLUMNS"] == "120"


def test_cli_run_returns_click_exit_code(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(cli, "_configure_terminal", lambda: None)
    monkeypatch.setattr(cli, "_invoke_app", lambda _argv: (_ for _ in ()).throw(cli.Exit(7)))

    assert cli.run([]) == 7


def test_cli_run_renders_click_errors_and_returns_their_code(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setattr(cli, "_configure_terminal", lambda: None)
    monkeypatch.setattr(
        cli,
        "_invoke_app",
        lambda _argv: (_ for _ in ()).throw(ClickException("broken")),
    )

    assert cli.run([]) == 1
    assert capsys.readouterr().err == "Error: broken\n"


def test_cli_run_reports_domain_errors_without_tracebacks(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setattr(cli, "_configure_terminal", lambda: None)
    monkeypatch.setattr(
        cli,
        "_invoke_app",
        lambda _argv: (_ for _ in ()).throw(ValueError("broken")),
    )

    assert cli.run([]) == 1
    assert capsys.readouterr().err == "error: broken\n"


def test_cli_run_returns_130_for_keyboard_interrupt(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(cli, "_configure_terminal", lambda: None)
    monkeypatch.setattr(
        cli,
        "_invoke_app",
        lambda _argv: (_ for _ in ()).throw(KeyboardInterrupt),
    )

    assert cli.run([]) == 130


def test_cli_run_and_publish_wires_tracker_logger_and_presenter(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    args = _cli_args(
        tmp_path,
        confirm_repo="owner/dataset",
        presenter=SimpleNamespace(observe=lambda _event: None),
    )
    tracker_roots: list[Path] = []
    tracker_instances: list[object] = []
    logger_calls: list[dict[str, object]] = []
    logger_instances: list[object] = []
    logger_closed: list[bool] = []
    workflow_calls: list[dict[str, object]] = []

    class FakeTracker:
        def __init__(self, *, data_root: Path) -> None:
            tracker_roots.append(data_root)
            tracker_instances.append(self)

    class FakeLogger:
        def __init__(self, **kwargs: object) -> None:
            logger_calls.append(kwargs)
            logger_instances.append(self)

        def close(self) -> None:
            logger_closed.append(True)

    report = SimpleNamespace(to_payload=lambda: {"status": "ok"})

    monkeypatch.setattr(cli, "TrackioRecorder", FakeTracker)
    monkeypatch.setattr(cli, "RunLogger", FakeLogger)
    monkeypatch.setattr(cli.uuid, "uuid4", lambda: "run-id")

    def workflow(**kwargs: object) -> SimpleNamespace:
        workflow_calls.append(kwargs)
        return report

    monkeypatch.setattr(cli, "run_and_publish", workflow)

    assert cli.handle_run_and_publish(args) == 0
    assert tracker_roots == [args.data_root]
    assert len(logger_calls) == 1
    assert logger_calls[0] == {
        "data_root": args.data_root,
        "run_id": "run-id",
        "buffer_preflight": True,
        "stderr": cli.sys.stderr,
        "observer": args.presenter.observe,
    }
    assert workflow_calls == [
        {
            "paths": cli._resolve_paths(args),
            "confirm_repo": "owner/dataset",
            "osmium_executable": "fake-osmium",
            "logger": logger_instances[0],
            "tracker": tracker_instances[0],
        }
    ]
    assert logger_closed == [True]
    assert json.loads(capsys.readouterr().out) == {"status": "ok"}


def test_cli_trackio_handler_reports_snapshot(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    args = _cli_args(tmp_path, project="p", space_id="o/s", run_name="snapshot-2026-08-20")
    expected = SimpleNamespace(
        to_payload=lambda: {
            "project": "p",
            "space_id": "o/s",
            "run_name": "snapshot-2026-08-20",
            "enabled": True,
        }
    )
    calls: list[tuple[object, object, object]] = []

    def publish(root: object, *, project: object, space_id: object, run_name: object) -> object:
        calls.append((root, project, space_id))
        assert root == args.data_root
        assert run_name == "snapshot-2026-08-20"
        return expected

    monkeypatch.setattr(cli, "publish_snapshot", publish)

    assert cli.handle_trackio_snapshot(args) == 0
    assert json.loads(capsys.readouterr().out) == expected.to_payload()
    assert calls == [(args.data_root, "p", "o/s")]


def test_cli_inspect_handler_reports_discovered_sources(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    args = _cli_args(tmp_path)
    source = SimpleNamespace(
        name="region.osm.pbf",
        output_name="region.parquet",
        size_bytes=12,
        mtime_ns=34,
    )
    monkeypatch.setattr(
        cli,
        "discover_sources",
        lambda root: [source] if root == args.source_root else [],
    )
    export_config = tmp_path / "export.json"
    monkeypatch.setattr(cli, "osmium_export_config", lambda: export_config)

    assert cli.handle_inspect(args) == 0
    assert json.loads(capsys.readouterr().out) == {
        "source_root": str(args.source_root),
        "data_root": str(args.data_root),
        "osmium_executable": "fake-osmium",
        "export_config": str(export_config),
        "source_count": 1,
        "sources": [
            {
                "name": "region.osm.pbf",
                "output_name": "region.parquet",
                "size_bytes": 12,
                "mtime_ns": 34,
            }
        ],
    }


def test_cli_build_all_handler_reports_each_result(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    args = _cli_args(tmp_path)
    result = SimpleNamespace(source_name="region.osm.pbf", status="built", included_rows=4)
    paths = SimpleNamespace(source_root=args.source_root)
    monkeypatch.setattr(
        cli,
        "_build_paths_and_executor",
        lambda value: (paths, lambda _source: result)
        if value is args
        else pytest.fail("wrong args"),
    )
    monkeypatch.setattr(
        cli,
        "discover_sources",
        lambda root: ["source"] if root == paths.source_root else [],
    )
    monkeypatch.setattr(
        cli, "build_all", lambda sources, build: [build(source) for source in sources]
    )

    assert cli.handle_build_all(args) == 0
    assert json.loads(capsys.readouterr().out) == {
        "count": 1,
        "results": [{"source_name": "region.osm.pbf", "status": "built", "included_rows": 4}],
    }


def test_cli_build_one_reports_every_result_field(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    args = _cli_args(tmp_path, basename="region.osm.pbf")
    source = SimpleNamespace(name=args.basename)
    result = SimpleNamespace(
        source_name=args.basename,
        output_name="region.parquet",
        status="built",
        emitted_features=9,
        included_rows=4,
        rejections={"invalid_geometry": 1},
        output_path=args.data_root / "data" / "region.parquet",
        manifest_path=args.data_root / "manifests" / "region.json",
    )
    paths = SimpleNamespace(source_root=args.source_root)
    selected: list[object] = []

    def executor(source_value: object) -> SimpleNamespace:
        selected.append(source_value)
        return result

    monkeypatch.setattr(
        cli,
        "_build_paths_and_executor",
        lambda value: (paths, executor) if value is args else pytest.fail("wrong args"),
    )
    monkeypatch.setattr(
        cli,
        "discover_sources",
        lambda root: [source] if root == paths.source_root else [],
    )

    assert cli.handle_build_one(args) == 0
    assert json.loads(capsys.readouterr().out) == {
        "source_name": args.basename,
        "output_name": "region.parquet",
        "status": "built",
        "emitted_features": 9,
        "included_rows": 4,
        "rejections": {"invalid_geometry": 1},
        "output_path": str(result.output_path),
        "manifest_path": str(result.manifest_path),
    }
    assert selected == [source]


def test_cli_validate_sorts_and_accumulates_every_parquet(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    args = _cli_args(tmp_path)
    data_dir = args.data_root / "data"
    data_dir.mkdir()
    first = data_dir / "a.parquet"
    second = data_dir / "b.parquet"
    first.write_bytes(b"a")
    second.write_bytes(b"b")
    calls: list[Path] = []

    def validate(path: Path) -> int:
        calls.append(path)
        return {first: 2, second: 3}[path]

    monkeypatch.setattr(cli, "validate_geoparquet", validate)
    original_glob = Path.glob

    def reverse_glob(path: Path, pattern: str) -> list[Path]:
        if path == data_dir and pattern == "*.parquet":
            return [second, first]
        return list(original_glob(path, pattern))

    monkeypatch.setattr(Path, "glob", reverse_glob)

    assert cli.handle_validate(args) == 0
    assert calls == [first, second]
    assert json.loads(capsys.readouterr().out) == {"files": 2, "rows": 5}


def test_cli_validate_reports_a_missing_data_directory(tmp_path: Path) -> None:
    args = _cli_args(tmp_path)

    with pytest.raises(ValueError, match="missing data directory"):
        cli.handle_validate(args)


def test_cli_card_passes_template_and_uses_empty_suffix_default(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    args = _cli_args(tmp_path)
    template = tmp_path / "template.md"
    captured: list[tuple[Path, Path]] = []

    monkeypatch.setattr(cli, "dataset_card_template", lambda: template)

    def generate(root: Path, value: Path) -> dict[str, object]:
        captured.append((root, value))
        return {"output_files": 0, "rows": 0}

    monkeypatch.setattr(cli, "generate_dataset_docs", generate)

    assert cli.handle_card(args) == 0
    assert captured == [(args.data_root, template)]
    assert json.loads(capsys.readouterr().out) == {
        "name_suffixes": {},
        "output_files": 0,
        "rows": 0,
    }


def test_cli_build_one_rejects_an_unknown_source(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    args = _cli_args(tmp_path, basename="missing.osm.pbf")
    paths = SimpleNamespace(source_root=args.source_root)
    monkeypatch.setattr(
        cli, "_build_paths_and_executor", lambda _value: (paths, lambda _source: None)
    )
    monkeypatch.setattr(cli, "discover_sources", lambda _root: [])

    with pytest.raises(ValueError, match="source not discovered: missing.osm.pbf"):
        cli.handle_build_one(args)


def test_cli_publish_handler_executes_the_confirmed_plan(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    args = _cli_args(tmp_path, plan="identity")
    plan = SimpleNamespace(repo_id="owner/dataset", identity_sha256="identity")
    calls: list[tuple[object, object]] = []
    monkeypatch.setattr(
        cli,
        "create_upload_plan",
        lambda root: plan if root == args.data_root else pytest.fail("wrong data root"),
    )
    monkeypatch.setattr(
        cli, "execute_upload", lambda current, confirmation: calls.append((current, confirmation))
    )

    assert cli.handle_publish(args) == 0
    assert calls == [(plan, "identity")]
    assert json.loads(capsys.readouterr().out) == {
        "repo_id": "owner/dataset",
        "identity_sha256": "identity",
    }


def test_cli_card_reports_stats_fields(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    args = _cli_args(tmp_path)
    stats = {"output_files": 2, "rows": 7, "name_suffixes": {"fr": 3}}
    monkeypatch.setattr(
        cli,
        "generate_dataset_docs",
        lambda root, _template: stats if root == args.data_root else pytest.fail("wrong data root"),
    )

    assert cli.handle_card(args) == 0
    assert json.loads(capsys.readouterr().out) == {
        "name_suffixes": {"fr": 3},
        "output_files": 2,
        "rows": 7,
    }


def test_cli_publish_plan_reports_each_file(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    args = _cli_args(tmp_path)
    plan = SimpleNamespace(
        repo_id="owner/dataset",
        identity_sha256="identity",
        files=(
            SimpleNamespace(relative_path="data/a.parquet", sha256="a"),
            SimpleNamespace(relative_path="README.md", sha256="b"),
        ),
    )
    monkeypatch.setattr(
        cli,
        "create_upload_plan",
        lambda root: plan if root == args.data_root else pytest.fail("wrong data root"),
    )

    assert cli.handle_publish_plan(args) == 0
    assert json.loads(capsys.readouterr().out) == {
        "repo_id": "owner/dataset",
        "identity_sha256": "identity",
        "files": [
            {"relative_path": "data/a.parquet", "sha256": "a"},
            {"relative_path": "README.md", "sha256": "b"},
        ],
    }


def test_cli_build_paths_executor_resolves_and_forwards_arguments(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    args = _cli_args(tmp_path)
    config = tmp_path / "osmium-export.json"
    built: list[tuple[object, object, str]] = []
    monkeypatch.setattr(cli, "osmium_export_config", lambda: config)

    def fake_build(source: object, paths: object, *, export_config: object, executable: str) -> str:
        built.append((source, export_config, executable))
        assert paths.data_root == args.data_root
        return "built"

    monkeypatch.setattr(cli, "build_one", fake_build)
    paths, executor = cli._build_paths_and_executor(args)

    assert paths.data_root == args.data_root
    assert executor("source") == "built"
    assert built == [("source", config, "fake-osmium")]


def test_cli_click_error_renderer_handles_usage_and_regular_errors(
    capsys: pytest.CaptureFixture[str],
) -> None:
    context = Context(Command("demo"))
    cli._show_click_error(UsageError("bad option", ctx=context))
    usage_output = capsys.readouterr().err
    assert usage_output == "usage:  [OPTIONS]\nerror: bad option\n"

    class _SpyClickError(ClickException):
        def __init__(self) -> None:
            super().__init__("broken")
            self.file: object = None

        def show(self, *, file: object = None) -> None:
            self.file = file

    error = _SpyClickError()
    cli._show_click_error(error)
    assert error.file is cli.sys.stderr


def test_cli_main_delegates_to_run(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(cli, "run", lambda: 7)
    with pytest.raises(SystemExit) as error:
        cli.main()
    assert error.value.code == 7


def test_empty_policy_hash_is_sha256_of_empty_bytes() -> None:
    assert _empty_policy_hash() == hashlib.sha256(b"").hexdigest()


def test_publish_retry_preserves_public_error_context() -> None:
    error = PublishRetry("retry", exit_code=503, kind="http")
    assert str(error) == "retry"
    assert error.exit_code == 503
    assert error.kind == "http"


def test_orchestrator_subprocess_bridge_restores_upload_runner(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import osm_polygon_description_tag.publication.upload as publication_upload

    original = publication_upload._default_runner_with_retry
    commands: list[list[str]] = []

    def fake_run_and_publish(**_kwargs: object) -> str:
        publication_upload._default_runner_with_retry(["hf", "upload"], timeout=1)
        return "ok"

    monkeypatch.setattr(orchestrator, "_run_and_publish", fake_run_and_publish)
    result = orchestrator._run_with_subprocess_bridge(commands.append)

    assert result == "ok"
    assert commands == [["hf", "upload"]]
    assert publication_upload._default_runner_with_retry is original


def test_default_hub_verifier_factory_uses_lazy_api(monkeypatch: pytest.MonkeyPatch) -> None:
    import osm_polygon_description_tag.publication.verification as verification

    class _Api:
        def whoami(self) -> dict[str, str]:
            return {"name": "tester"}

        def repo_info(self, _repo_id: str, **_kwargs: object) -> SimpleNamespace:
            return SimpleNamespace(sha="revision")

    monkeypatch.setattr(verification._huggingface_hub, "HfApi", _Api)
    verifier = verification.default_hub_verifier_factory()
    assert verifier("owner/dataset", ()) == "revision"
