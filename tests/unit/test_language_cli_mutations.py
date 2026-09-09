"""Focused language CLI contracts for surviving mutation protection."""

import io
import json
from contextlib import nullcontext
from pathlib import Path
from types import SimpleNamespace

import pytest

from osm_polygon_description_tag import language_cli
from osm_polygon_description_tag.dataset.languages.detector import LanguageDetector
from osm_polygon_description_tag.dataset.languages.models import (
    V2_LANGUAGE_POLICY,
    LanguagePolicy,
    language_model_identity,
)


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
        0.91,
        0.13,
    )

    assert received == {
        "source_root": Path("/source"),
        "run_dir": Path("/run"),
        "project_root": Path("/project"),
        "policy": LanguagePolicy(min_alphabetic_chars=17, min_score=0.91, min_margin=0.13),
    }


def test_prepare_command_selects_the_named_v2_policy(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    received: dict[str, object] = {}

    def fake_handle_prepare(
        source_root: Path,
        run_dir: Path,
        project_root: Path,
        policy: LanguagePolicy,
    ) -> None:
        received["policy"] = policy

    monkeypatch.setattr(language_cli, "handle_prepare", fake_handle_prepare)

    language_cli.prepare_command(
        Path("/source"),
        Path("/run"),
        Path("/project"),
        policy_version="v2",
    )

    assert received["policy"] == V2_LANGUAGE_POLICY


def test_handle_prepare_emits_complete_snapshot_metadata(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    source_root = Path("/source")
    run_dir = Path("/run")
    project_root = Path("/project")
    policy = LanguagePolicy(min_alphabetic_chars=9, min_score=0.87, min_margin=0.14)
    snapshot = SimpleNamespace(
        snapshot_id="snapshot-123",
        source_files=(
            SimpleNamespace(relative_path="north.parquet", row_count=4),
            SimpleNamespace(relative_path="south.parquet", row_count=6),
        ),
        model_config_fingerprint="config-456",
        model_identity=SimpleNamespace(library_name="fake-detector", library_version="1.2.3"),
    )
    received: dict[str, object] = {}

    def fake_prepare_snapshot(
        received_source_root: Path,
        received_run_dir: Path,
        *,
        code_fingerprint: str,
        lock_fingerprint: str,
        policy: LanguagePolicy,
    ) -> object:
        received.update(
            source_root=received_source_root,
            run_dir=received_run_dir,
            code_fingerprint=code_fingerprint,
            lock_fingerprint=lock_fingerprint,
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
        "policy": policy,
    }
    assert _payload(capsys) == {
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
    policy = LanguagePolicy(min_alphabetic_chars=8, min_score=0.88, min_margin=0.16)
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

    language_cli.handle_run(source_root, run_dir, shard, 3, 12.5)

    assert received["detector_policy"] == policy
    assert received["language_codes"] is None
    assert seen_detector is not None
    assert isinstance(received["process"], dict)
    assert received["process"] == {
        "run_dir": run_dir,
        "source_root": source_root,
        "shard": shard,
        "detector": seen_detector,
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
