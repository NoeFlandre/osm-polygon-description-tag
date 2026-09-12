"""Exact argument forwarding and payloads of the ``language grid`` commands.

Every one of these commands is driven by an operator against one of 386 shards
on a shared cluster, and each delegates to a boundary that is faked in tests. A
fake that accepts any arguments proves only that a call happened, so these pin
what was actually passed: the wrong run directory, the wrong bundle, or a
dropped batch size would submit a job that silently computes the wrong thing.
"""

from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from osm_polygon_description_tag import language_cli
from osm_polygon_description_tag.dataset.languages.models import (
    CASCADE_DETECTOR_NAME,
    DEFAULT_LANGUAGE_SCOPE,
    LanguagePolicy,
    language_model_identity,
)
from osm_polygon_description_tag.dataset.languages.snapshot import SnapshotError
from osm_polygon_description_tag.workflow.grid_operator import GridOperatorError
from tests.helpers.messages import exactly

SHARD = "region.parquet"


def _stdout(capsys: pytest.CaptureFixture[str]) -> dict[str, Any]:
    return json.loads(capsys.readouterr().out)


def _report(**payload: object) -> SimpleNamespace:
    return SimpleNamespace(to_payload=lambda: dict(payload))


def test_the_default_policy_preset_is_the_v1_preset() -> None:
    """The default must stay ``v1``; a corrupted default would be refused outright."""
    assert language_cli._policy(None, None, None) == language_cli._POLICY_PRESETS["v1"]


@pytest.mark.parametrize(
    ("base", "expected"),
    [
        (
            "/home/user/bundle",
            {
                "remote_bundle_dir": "/home/user/bundle",
                "remote_project_dir": "/home/user/bundle/project",
                "remote_run_dir": "/home/user/bundle/run",
                "remote_source_dir": "/home/user/bundle/source",
            },
        ),
        (
            "/home/user/bundle///",
            {
                "remote_bundle_dir": "/home/user/bundle",
                "remote_project_dir": "/home/user/bundle/project",
                "remote_run_dir": "/home/user/bundle/run",
                "remote_source_dir": "/home/user/bundle/source",
            },
        ),
        (
            "/home/user/bundleX",
            {
                "remote_bundle_dir": "/home/user/bundleX",
                "remote_project_dir": "/home/user/bundleX/project",
                "remote_run_dir": "/home/user/bundleX/run",
                "remote_source_dir": "/home/user/bundleX/source",
            },
        ),
        (
            "///",
            {
                "remote_bundle_dir": "/",
                "remote_project_dir": "/project",
                "remote_run_dir": "/run",
                "remote_source_dir": "/source",
            },
        ),
    ],
)
def test_the_portable_remote_paths_are_exact_for_any_bundle_root(
    base: str, expected: dict[str, str]
) -> None:
    """These four strings become the job script's own paths on the compute node."""
    assert language_cli._portable_remote_paths(base) == expected


def test_a_transport_timeout_is_refused_with_its_exact_reason() -> None:
    timed_out = SimpleNamespace(timed_out=True, returncode=-1, stderr="")

    with pytest.raises(GridOperatorError, match=exactly("transport command timed out")):
        language_cli._raise_transport_failure(timed_out)  # type: ignore[arg-type]


def test_a_model_identity_of_the_wrong_type_is_refused_with_its_exact_reason() -> None:
    with pytest.raises(TypeError, match=exactly("model_identity must be a LanguageModelIdentity")):
        language_cli._prepare_identity(LanguagePolicy(), object())  # type: ignore[arg-type]


def test_a_model_identity_for_another_policy_is_refused_with_its_exact_reason() -> None:
    identity = language_model_identity(LanguagePolicy())
    other = LanguagePolicy(min_alphabetic_chars=LanguagePolicy().min_alphabetic_chars + 1)

    with pytest.raises(
        SnapshotError,
        match=exactly("model identity policy does not match the requested policy"),
    ):
        language_cli._prepare_identity(other, identity)


def test_preparing_a_snapshot_forwards_the_requested_identity_unchanged(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A dropped identity would freeze the snapshot against the wrong detector."""
    identity = language_model_identity(LanguagePolicy())
    seen: list[object] = []

    def fake_prepare_identity(policy: object, model_identity: object) -> object:
        seen.append((policy, model_identity))
        return identity

    monkeypatch.setattr(language_cli, "_prepare_identity", fake_prepare_identity)
    monkeypatch.setattr(
        language_cli,
        "prepare_snapshot",
        lambda *_args, **_kwargs: SimpleNamespace(
            snapshot_id="s" * 64,
            source_files=(),
            to_payload=lambda: {},
        ),
    )
    monkeypatch.setattr(language_cli, "print_json", lambda _payload: None)
    monkeypatch.setattr(language_cli, "_prepare_report", lambda *_args: {})
    monkeypatch.setattr(language_cli, "fingerprint_project_source", lambda _root: "a" * 64)
    monkeypatch.setattr(language_cli, "fingerprint_lockfile", lambda _root: "b" * 64)

    language_cli.handle_prepare(
        Path("/source"),
        Path("/run"),
        Path("/project"),
        LanguagePolicy(),
        model_identity=identity,
    )

    assert seen == [(LanguagePolicy(), identity)]


def test_a_lingua_snapshot_refuses_a_glotlid_model_path_with_its_exact_reason(
    tmp_path: Path,
) -> None:
    identity = language_model_identity(LanguagePolicy())

    with pytest.raises(
        SnapshotError, match=exactly("GlotLID model path requires a cascade snapshot")
    ):
        language_cli._build_detector_for_snapshot(
            identity, glotlid_model_path=tmp_path / "model.bin"
        )


def test_an_unknown_detector_in_a_snapshot_names_the_detector_it_refused() -> None:
    identity = SimpleNamespace(
        detector_name="mystery",
        language_scope=DEFAULT_LANGUAGE_SCOPE,
        policy=LanguagePolicy(),
    )

    with pytest.raises(SnapshotError, match=exactly("unsupported detector in snapshot: 'mystery'")):
        language_cli._build_detector_for_snapshot(identity, glotlid_model_path=None)  # type: ignore[arg-type]


def test_a_narrowed_language_scope_reaches_the_constructed_cascade_detector(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Dropping the scope would detect languages the snapshot excluded."""
    scope = ("eng", "fra")
    identity = SimpleNamespace(
        detector_name=CASCADE_DETECTOR_NAME,
        language_scope=scope,
        policy=LanguagePolicy(),
    )
    seen: list[object] = []

    def fake_build(policy: object, **kwargs: object) -> object:
        seen.append((policy, kwargs))
        return object()

    monkeypatch.setattr(language_cli, "build_language_detector", fake_build)

    language_cli._build_detector_for_snapshot(identity, glotlid_model_path=None)  # type: ignore[arg-type]

    assert seen == [
        (identity.policy, {"language_codes": scope, "glotlid_model_path": None}),
    ]


def test_local_collection_reports_the_run_directory_it_validated(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """The payload is an operator's record of which run directory was read."""
    monkeypatch.setattr(
        language_cli, "collect_results", lambda *_args: _report(complete=True, issues=[])
    )

    language_cli.handle_grid_collect(tmp_path / "run", SHARD)

    assert _stdout(capsys) == {
        "complete": True,
        "issues": [],
        "run_dir": str(tmp_path / "run"),
    }


def _install_collection_fakes(
    monkeypatch: pytest.MonkeyPatch, observed: list[object], report: object, bundle: object
) -> object:
    snapshot = object()
    paths = object()
    acknowledgment = _report(result_acknowledged=True, terminal_state="terminated")

    def fake_import(local_run: Path, incoming: Path, shard: str) -> object:
        observed.append(("import", local_run, incoming, shard))
        return report

    def fake_read_snapshot(run_dir: Path) -> object:
        observed.append(("snapshot", run_dir))
        return snapshot

    def fake_bundle_for_shard(received_snapshot: object, shard: str) -> object:
        observed.append(("bundle", received_snapshot, shard))
        return bundle

    def fake_job_paths(run_dir: Path, received_bundle: object) -> object:
        observed.append(("paths", run_dir, received_bundle))
        return paths

    def fake_ack(received_paths: object, received_report: object) -> object:
        observed.append(("ack", received_paths, received_report))
        return acknowledgment

    monkeypatch.setattr(language_cli, "import_retrieved_results", fake_import, raising=False)
    monkeypatch.setattr(language_cli, "read_snapshot", fake_read_snapshot)
    monkeypatch.setattr(language_cli, "bundle_for_shard", fake_bundle_for_shard)
    monkeypatch.setattr(language_cli, "job_paths", fake_job_paths)
    monkeypatch.setattr(language_cli, "acknowledge_collected_results", fake_ack, raising=False)
    return snapshot, paths


def test_importing_a_retrieved_run_acknowledges_this_run_and_this_shard(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """Acknowledging the wrong shard's job would lose a completed shard's results."""
    run_dir = tmp_path / "run"
    retrieved = tmp_path / "retrieved"
    report = _report(complete=True, annotation_count=6)
    bundle = object()
    observed: list[object] = []
    snapshot, paths = _install_collection_fakes(monkeypatch, observed, report, bundle)

    language_cli.handle_grid_collect(run_dir, SHARD, retrieved_run_dir=retrieved)

    assert observed == [
        ("import", run_dir, retrieved, SHARD),
        ("snapshot", run_dir),
        ("bundle", snapshot, SHARD),
        ("paths", run_dir, bundle),
        ("ack", paths, report),
    ]
    assert _stdout(capsys)["applied"] is False


@pytest.mark.parametrize(
    ("remote_bundle_dir", "remote_run_dir"),
    [
        ("/home/user/bundleX///", "/home/user/bundleX/run"),
        ("///", "/run"),
    ],
)
def test_a_retrieval_transfers_the_remote_run_child_of_the_bundle_root(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    remote_bundle_dir: str,
    remote_run_dir: str,
) -> None:
    """The retrieval must read the bundle's ``run`` child, not the bundle itself."""
    run_dir = tmp_path / "run"
    run_dir.mkdir()
    (run_dir / "snapshot.json").write_text("{}\n", encoding="utf-8")
    retrieved = tmp_path / "retrieved"
    report = _report(complete=True)
    bundle = object()
    observed: list[object] = []
    snapshot, paths = _install_collection_fakes(monkeypatch, observed, report, bundle)
    argv_calls: list[object] = []

    def fake_argv(remote_dir: str, local_dir: Path, shard: str) -> tuple[str, ...]:
        argv_calls.append((remote_dir, local_dir, shard))
        return ("rsync", "--archive")

    monkeypatch.setattr(language_cli, "build_result_retrieval_argv", fake_argv)

    def fake_runner(argv: tuple[str, ...], timeout: float) -> SimpleNamespace:
        return SimpleNamespace(
            returncode=0,
            stdout="",
            stderr="",
            timed_out=False,
            to_payload=lambda: {"returncode": 0},
        )

    language_cli.handle_grid_collect(
        run_dir,
        SHARD,
        remote_bundle_dir=remote_bundle_dir,
        retrieved_run_dir=retrieved,
        apply=True,
        runner=fake_runner,
    )

    assert argv_calls == [(remote_run_dir, retrieved, SHARD)]
    assert observed == [
        ("import", run_dir, retrieved, SHARD),
        ("snapshot", run_dir),
        ("bundle", snapshot, SHARD),
        ("paths", run_dir, bundle),
        ("ack", paths, report),
    ]
    assert _stdout(capsys)["applied"] is True


def test_a_retrieval_imports_nothing_unless_the_apply_gate_is_passed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """Collection defaults to a dry run; importing by default would apply silently."""
    run_dir = tmp_path / "run"
    run_dir.mkdir()
    (run_dir / "snapshot.json").write_text("{}\n", encoding="utf-8")
    retrieved = tmp_path / "retrieved"
    observed: list[object] = []
    _install_collection_fakes(monkeypatch, observed, _report(complete=True), object())
    monkeypatch.setattr(
        language_cli, "build_result_retrieval_argv", lambda *_args: ("rsync", "--archive")
    )

    language_cli.handle_grid_collect(
        run_dir,
        SHARD,
        remote_bundle_dir="/home/user/bundle",
        retrieved_run_dir=retrieved,
        runner=lambda argv, timeout: SimpleNamespace(
            returncode=0,
            stdout="",
            stderr="",
            timed_out=False,
            to_payload=lambda: {"returncode": 0},
        ),
    )

    assert observed == []
    assert _stdout(capsys)["applied"] is False


def test_a_staged_payload_directory_with_the_stable_name_is_a_candidate(
    tmp_path: Path,
) -> None:
    """The stable payload directory is the one a repeated submission reuses."""
    root = tmp_path / "job"
    root.mkdir()
    (root / "payload").mkdir()
    (root / "payload-0001").mkdir()
    (root / "bundle.json").write_text("{}", encoding="utf-8")
    paths = SimpleNamespace(root=root, payload_root=root / "payload")

    candidates = language_cli._portable_payload_candidates(paths)  # type: ignore[arg-type]

    assert candidates == (root / "payload", root / "payload-0001")


def test_preparing_a_job_forwards_the_requested_budget_and_reports_its_directory(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """A dropped budget would submit a job that computes a different amount of work."""
    job_root = tmp_path / "run" / "jobs" / ("b" * 32)
    paths = SimpleNamespace(root=job_root, script=job_root / "job.sh")
    bundle = SimpleNamespace(bundle_id="bundle-prepare", shard=SHARD, input_row_count=12)
    seen: list[object] = []

    def fake_prepare_job(*args: object, **kwargs: object) -> object:
        seen.append(kwargs)
        return bundle, paths

    monkeypatch.setattr(language_cli, "read_snapshot", lambda _run: object())
    monkeypatch.setattr(language_cli, "prepare_job", fake_prepare_job)

    language_cli.handle_grid_prepare(
        tmp_path / "run",
        SHARD,
        "/remote/project",
        "/remote/source",
        "/remote/run",
        900,
        64,
    )

    assert seen == [
        {
            "remote_project_dir": "/remote/project",
            "remote_source_dir": "/remote/source",
            "remote_run_dir": "/remote/run",
            "processing_seconds": 900,
            "batch_size": 64,
            "glotlid_model_path": None,
        }
    ]
    assert _stdout(capsys) == {
        "bundle_id": "bundle-prepare",
        "shard": SHARD,
        "input_row_count": 12,
        "job_dir": str(job_root),
        "script": str(job_root / "job.sh"),
    }


def test_reconciling_a_job_forwards_the_apply_gate_it_was_given(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """A forced apply would write terminal state the operator did not authorise."""
    seen: list[object] = []
    reconciliation = SimpleNamespace(
        to_payload=lambda: {"state": "unknown", "detail": "resolved by name"}
    )

    def fake_reconcile(paths: object, *, apply: object, **_kwargs: object) -> object:
        seen.append(apply)
        return reconciliation

    monkeypatch.setattr(language_cli, "read_snapshot", lambda _run: object())
    monkeypatch.setattr(
        language_cli, "bundle_for_shard", lambda *_args: SimpleNamespace(bundle_id="bundle-status")
    )
    monkeypatch.setattr(language_cli, "job_paths", lambda *_args: object())
    monkeypatch.setattr(language_cli, "reconcile_job", fake_reconcile)

    language_cli.handle_grid_status(tmp_path / "run", SHARD, False)
    language_cli.handle_grid_status(tmp_path / "run", SHARD, True)
    capsys.readouterr()

    assert seen == [False, True]


def test_a_reused_staged_payload_is_verified_against_this_shards_bundle(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Verifying against no bundle would accept another shard's staged payload."""
    run_dir = tmp_path / "run"
    job_root = run_dir / "jobs" / ("b" * 32)
    payload_root = job_root / "payload"
    payload_root.mkdir(parents=True)
    for name in ("bundle.json", "config.json", "job.sh"):
        (job_root / name).write_text("x", encoding="utf-8")
        (payload_root / name).write_text("x", encoding="utf-8")
    bundle = object()
    paths = SimpleNamespace(
        root=job_root,
        payload_root=payload_root,
        bundle=job_root / "bundle.json",
        config=job_root / "config.json",
        script=job_root / "job.sh",
    )
    seen: list[object] = []

    def fake_verify(payload: Path, *, expected_bundle: object) -> None:
        seen.append((payload, expected_bundle))

    monkeypatch.setattr(language_cli, "bundle_for_shard", lambda *_args: bundle)
    monkeypatch.setattr(language_cli, "job_paths", lambda *_args: paths)
    monkeypatch.setattr(language_cli, "verify_prepared_bundle", fake_verify)
    monkeypatch.setattr(language_cli, "prepare_job", lambda *_args, **_kwargs: (bundle, paths))

    language_cli._reuse_verified_staged_job(
        run_dir,
        object(),  # type: ignore[arg-type]
        SHARD,
        remote_project_dir="/remote/project",
        remote_source_dir="/remote/source",
        remote_run_dir="/remote/run",
        processing_seconds=900,
        batch_size=64,
        walltime_seconds=1800,
    )

    assert seen == [(payload_root, bundle)]


def test_submitting_forwards_the_budget_and_the_daytime_authorisation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """Daytime accounting and the work budget are both the operator's explicit choice."""
    run_dir = tmp_path / "run"
    paths = SimpleNamespace(root=run_dir / "jobs")
    bundle = object()
    prepared: list[object] = []
    policies: list[object] = []

    def fake_reuse(*args: object, **kwargs: object) -> object:
        prepared.append(kwargs)
        return bundle, paths

    def fake_evaluate(**kwargs: object) -> object:
        policies.append(kwargs["allow_daytime"])
        return SimpleNamespace(decision="allowed")

    monkeypatch.setattr(language_cli, "read_snapshot", lambda _run: object())
    monkeypatch.setattr(language_cli, "_reuse_verified_staged_job", fake_reuse)
    monkeypatch.setattr(language_cli, "evaluate_policy", fake_evaluate)
    monkeypatch.setattr(
        language_cli,
        "submit_job",
        lambda *_args, **_kwargs: (SimpleNamespace(to_payload=lambda: {}), None),
    )

    language_cli.handle_grid_submit(
        run_dir,
        SHARD,
        "/remote/project",
        "/remote/source",
        "/remote/run",
        "nancy",
        1800,
        900,
        64,
        True,
        False,
    )
    capsys.readouterr()

    assert prepared == [
        {
            "remote_project_dir": "/remote/project",
            "remote_source_dir": "/remote/source",
            "remote_run_dir": "/remote/run",
            "processing_seconds": 900,
            "batch_size": 64,
            "walltime_seconds": 1800,
            "glotlid_model_path": None,
        }
    ]
    assert policies == [True]


def test_a_first_submission_prepares_the_job_with_the_requested_budget(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """With nothing staged yet, the fallback preparation carries the same settings."""
    run_dir = tmp_path / "run"
    paths = SimpleNamespace(root=run_dir / "jobs")
    bundle = object()
    prepared: list[object] = []

    def fake_prepare_job(*args: object, **kwargs: object) -> object:
        prepared.append(kwargs)
        return bundle, paths

    monkeypatch.setattr(language_cli, "read_snapshot", lambda _run: object())
    monkeypatch.setattr(language_cli, "_reuse_verified_staged_job", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(language_cli, "prepare_job", fake_prepare_job)
    monkeypatch.setattr(
        language_cli, "evaluate_policy", lambda **_kwargs: SimpleNamespace(decision="allowed")
    )
    monkeypatch.setattr(
        language_cli,
        "submit_job",
        lambda *_args, **_kwargs: (SimpleNamespace(to_payload=lambda: {}), None),
    )

    language_cli.handle_grid_submit(
        run_dir,
        SHARD,
        "/remote/project",
        "/remote/source",
        "/remote/run",
        "nancy",
        1800,
        900,
        64,
        False,
        False,
    )
    capsys.readouterr()

    assert prepared == [
        {
            "remote_project_dir": "/remote/project",
            "remote_source_dir": "/remote/source",
            "remote_run_dir": "/remote/run",
            "processing_seconds": 900,
            "batch_size": 64,
            "walltime_seconds": 1800,
            "glotlid_model_path": None,
            "remote_bundle_dir": None,
        }
    ]
