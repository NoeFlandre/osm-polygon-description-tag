"""Contracts of the multi-shard Grid'5000 driver (``language grid run``).

The driver may only sequence the CLI; it must never decide to spend resources.
These tests pin the three properties that make that true: it halts instead of
resubmitting anything ambiguous, it never enables daytime submission on its
own, and it processes the snapshot's shards in the snapshot's own order.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any

import pytest

import osm_polygon_description_tag.workflow.grid_driver as driver
from osm_polygon_description_tag.workflow.grid_driver import (
    _UNREADABLE_CHECKPOINT,
    DriverError,
    DriverOptions,
    Remote,
    _cli,
    _shard_slug,
    awaiting_collection,
    checkpointed_shards,
    selected_shards,
    shards_of,
    submit,
)


def _remote() -> Remote:
    return Remote(
        "nancy",
        "/home/op/bundles",
        "/home/op/models/glotlid-v3/model_v3.bin",
        "/home/op/models/sat-3l-sm/model.safetensors",
    )


def _args(**overrides: Any) -> DriverOptions:
    fields: dict[str, Any] = {
        "run_dir": Path("/run"),
        "source_root": Path("/source"),
        "retrieval_dir": Path("/retrieve"),
        "remote_bundle_root": "/home/op/bundles",
        "remote_glotlid_model_path": "/home/op/models/glotlid-v3/model_v3.bin",
        "remote_sat_model_path": "/home/op/models/sat-3l-sm/model.safetensors",
        "remote_operator_dir": "/home/op/project",
        "remote_cli": "/home/op/.venv/bin/osm-polygon-description-tag",
    }
    return DriverOptions(**{**fields, **overrides})


def _plan() -> dict[str, str]:
    return {
        "remote_project_dir": "/home/op/bundles/x/project",
        "remote_source_dir": "/home/op/bundles/x/source",
        "remote_run_dir": "/home/op/bundles/x/run",
    }


@pytest.mark.parametrize("outcome", ["ambiguous", "rejected", "", "SUBMITTED_MAYBE"])
def test_a_submission_that_is_not_cleanly_submitted_halts_the_run(
    outcome: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    """An ambiguous submission means a job may exist; the driver must stop."""
    monkeypatch.setattr(
        "osm_polygon_description_tag.workflow.grid_driver._ssh_json",
        lambda *args, **kwargs: {"outcome": outcome, "job_id": None},
    )

    with pytest.raises(DriverError) as caught:
        submit(_args(), _remote(), "region.parquet", _plan())

    assert "do not resubmit" in str(caught.value)


def test_daytime_submission_is_off_unless_the_operator_asks_for_it(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    sent: list[str] = []

    def fake_ssh(remote: Remote, command: str) -> dict[str, Any]:
        sent.append(command)
        return _apply_payload("submitted")

    monkeypatch.setattr("osm_polygon_description_tag.workflow.grid_driver._ssh_json", fake_ssh)

    submit(_args(), _remote(), "region.parquet", _plan())

    assert "--allow-daytime" not in sent[0]
    assert "--apply" in sent[0]


def test_daytime_submission_is_forwarded_only_when_explicitly_requested(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    sent: list[str] = []

    def fake_ssh(remote: Remote, command: str) -> dict[str, Any]:
        sent.append(command)
        return _apply_payload("submitted")

    monkeypatch.setattr("osm_polygon_description_tag.workflow.grid_driver._ssh_json", fake_ssh)

    submit(_args(allow_daytime=True), _remote(), "region.parquet", _plan())

    assert "--allow-daytime" in sent[0]


def test_the_driver_defaults_to_one_core_sized_bounded_jobs() -> None:
    args = _args()

    assert args.walltime_seconds == 1800
    assert args.processing_seconds == 1200
    assert args.processing_seconds < args.walltime_seconds
    assert args.batch_size == 512
    assert args.allow_daytime is False


def test_shards_are_read_in_snapshot_order_with_their_row_counts(tmp_path: Path) -> None:
    (tmp_path / "snapshot.json").write_text(
        json.dumps(
            {
                "source_files": [
                    {"relative_path": "b.parquet", "row_count": 2},
                    {"relative_path": "a.parquet", "row_count": 1},
                ]
            }
        ),
        encoding="utf-8",
    )

    assert shards_of(tmp_path) == (("b.parquet", 2), ("a.parquet", 1))


@pytest.mark.parametrize(
    ("shard", "slug"),
    [
        ("region.parquet", "region-parquet"),
        ("a/b c.parquet", "a-b-c-parquet"),
        ("$(evil).parquet", "evil--parquet"),
        ("X.parquetX", "X-parquetX"),
    ],
)
def test_a_remote_directory_name_cannot_carry_shell_metacharacters(shard: str, slug: str) -> None:
    """OAR evaluates stored commands through a shell, so the name must be inert."""
    result = _shard_slug(shard)

    assert result == slug
    assert all(character.isalnum() or character == "-" for character in result)


def test_the_driver_stages_and_forwards_the_pinned_sat_model_path() -> None:
    """Splitting runs in the same job, so the driver must name the SaT weights."""
    remote = _remote()

    assert remote.sat_model_path == "/home/op/models/sat-3l-sm/model.safetensors"


def test_every_remote_model_path_is_required_by_the_driver() -> None:
    """A run that forgets either artifact must fail on the frontend, not the node."""
    args = _args()

    assert args.remote_sat_model_path == "/home/op/models/sat-3l-sm/model.safetensors"
    assert args.remote_glotlid_model_path == "/home/op/models/glotlid-v3/model_v3.bin"


def _apply_payload(outcome: str, *, job_id: int | None = 6923144) -> dict[str, Any]:
    """Return the payload ``language grid submit --apply`` actually prints.

    The CLI wraps the submission under ``result``; it does not put ``outcome``
    at the top level. A driver that reads the top level sees no outcome at all
    and halts a run whose job is genuinely queued, which is the worst possible
    reading: the job exists and the operator is told it does not.
    """
    return {
        "applied": True,
        "plan": {"shard": "region.parquet", "may_apply": True},
        "result": {
            "outcome": outcome,
            "job_id": job_id,
            "detail": "job submitted",
            "attempt": 1,
        },
    }


def test_a_clean_submission_in_the_cli_payload_shape_is_accepted(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A queued job must be recognised from the shape the CLI really emits."""
    monkeypatch.setattr(
        "osm_polygon_description_tag.workflow.grid_driver._ssh_json",
        lambda *args, **kwargs: _apply_payload("submitted"),
    )

    submit(_args(), _remote(), "region.parquet", _plan())


def test_the_recognised_submission_reports_the_scheduler_job_id(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """The operator needs the job id to reconcile; it must reach the log."""
    monkeypatch.setattr(
        "osm_polygon_description_tag.workflow.grid_driver._ssh_json",
        lambda *args, **kwargs: _apply_payload("submitted"),
    )

    submit(_args(), _remote(), "region.parquet", _plan())

    logged = json.loads(capsys.readouterr().out.strip().splitlines()[-1])
    assert logged["event"] == "submitted"
    assert logged["job_id"] == 6923144


@pytest.mark.parametrize("outcome", ["ambiguous", "rejected", "", "SUBMITTED_MAYBE"])
def test_an_unclean_outcome_inside_the_cli_payload_halts_the_run(
    outcome: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Reading the nested outcome must not lose the ambiguity check."""
    monkeypatch.setattr(
        "osm_polygon_description_tag.workflow.grid_driver._ssh_json",
        lambda *args, **kwargs: _apply_payload(outcome),
    )

    with pytest.raises(DriverError) as caught:
        submit(_args(), _remote(), "region.parquet", _plan())

    assert "do not resubmit" in str(caught.value)


def test_a_refused_apply_gate_halts_the_run(monkeypatch: pytest.MonkeyPatch) -> None:
    """``result`` is null when the gate refused; nothing was submitted."""
    monkeypatch.setattr(
        "osm_polygon_description_tag.workflow.grid_driver._ssh_json",
        lambda *args, **kwargs: {
            "applied": False,
            "plan": {"blocked_reason": "daytime submission is refused"},
            "result": None,
        },
    )

    with pytest.raises(DriverError) as caught:
        submit(_args(), _remote(), "region.parquet", _plan())

    assert "do not resubmit" in str(caught.value)


def test_the_whole_snapshot_is_selected_by_default() -> None:
    """Without a partition the driver must still see every shard."""
    shards = (("a.parquet", 1), ("b.parquet", 2), ("c.parquet", 3))

    assert selected_shards(shards, stride=1, index=0) == shards


@pytest.mark.parametrize("index", [0, 1, 2])
def test_a_partition_keeps_the_snapshot_order_within_its_share(index: int) -> None:
    shards = tuple((f"{n}.parquet", n) for n in range(9))

    chosen = selected_shards(shards, stride=3, index=index)

    assert chosen == tuple(shards[position] for position in range(index, 9, 3))


def test_partitions_of_one_stride_are_disjoint_and_cover_everything() -> None:
    """Two drivers must never pick the same shard, and none may be dropped."""
    shards = tuple((f"{n}.parquet", n) for n in range(20))

    parts = [selected_shards(shards, stride=4, index=index) for index in range(4)]

    flattened = [entry for part in parts for entry in part]
    assert sorted(flattened) == sorted(shards)
    assert len(flattened) == len(set(flattened))


@pytest.mark.parametrize(
    ("stride", "index"),
    [(0, 0), (-1, 0), (2, 2), (2, -1), (2, 5)],
)
def test_an_impossible_partition_is_refused(stride: int, index: int) -> None:
    """A bad partition would silently drop or double-process shards."""
    with pytest.raises(DriverError) as caught:
        selected_shards((("a.parquet", 1),), stride=stride, index=index)

    assert "partition" in str(caught.value)


def test_the_partition_defaults_to_the_whole_snapshot() -> None:
    args = _args()

    assert args.shard_stride == 1
    assert args.shard_index == 0


def test_no_queue_is_requested_unless_the_operator_names_one(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Some sites reject an explicit queue, so the bare form stays the default."""
    sent: list[str] = []

    def fake_ssh(remote: Remote, command: str) -> dict[str, Any]:
        sent.append(command)
        return _apply_payload("submitted")

    monkeypatch.setattr("osm_polygon_description_tag.workflow.grid_driver._ssh_json", fake_ssh)

    submit(_args(), _remote(), "region.parquet", _plan())

    assert "--queue" not in sent[0]


def test_a_named_queue_is_forwarded_to_the_cli(monkeypatch: pytest.MonkeyPatch) -> None:
    """Four of the eight sites cannot be used at all without this."""
    sent: list[str] = []

    def fake_ssh(remote: Remote, command: str) -> dict[str, Any]:
        sent.append(command)
        return _apply_payload("submitted")

    monkeypatch.setattr("osm_polygon_description_tag.workflow.grid_driver._ssh_json", fake_ssh)

    submit(_args(queue="default"), _remote(), "region.parquet", _plan())

    assert "--queue default" in sent[0]


def test_the_driver_never_shells_out_through_uv() -> None:
    """Every CLI call must go straight to the interpreter's own console script.

    ``uv run`` takes a lock on the shared uv cache for the duration of the
    command. The driver invokes the CLI once per shard to test completeness, so
    routing those through ``uv run`` makes a parent holding that lock spawn a
    child that waits for it. With several drivers in parallel the run stops
    dead: seven sweep workers sat for thirty minutes with no child process at
    all. Calling the console script beside ``sys.executable`` removes the lock
    from the hot path, and a process start with it.
    """
    argv = _cli()

    assert "uv" not in argv
    assert Path(argv[0]).name == "osm-polygon-description-tag"
    assert Path(argv[0]).parent == Path(sys.executable).parent


def _write_checkpoint(run_dir: Path, slug: str, payload: object) -> Path:
    shard_dir = run_dir / "shards" / slug
    shard_dir.mkdir(parents=True)
    checkpoint = shard_dir / "checkpoint.json"
    if isinstance(payload, str):
        checkpoint.write_text(payload, encoding="utf-8")
    else:
        checkpoint.write_text(json.dumps(payload), encoding="utf-8")
    return checkpoint


def test_checkpointed_shards_is_empty_without_a_shards_directory(tmp_path: Path) -> None:
    """A run that has never written a shard cannot have a complete one."""
    assert checkpointed_shards(tmp_path) == frozenset()


def test_checkpointed_shards_reports_every_checkpointed_shard_name(tmp_path: Path) -> None:
    _write_checkpoint(tmp_path, "aaa", {"shard": "albania-latest.parquet", "status": "complete"})
    _write_checkpoint(tmp_path, "bbb", {"shard": "angola-latest.parquet", "status": "partial"})

    assert checkpointed_shards(tmp_path) == frozenset(
        {"albania-latest.parquet", "angola-latest.parquet"}
    )


def test_checkpointed_shards_reports_a_partial_checkpoint_too(tmp_path: Path) -> None:
    """The scan narrows who is asked; it must not judge completeness itself."""
    _write_checkpoint(tmp_path, "aaa", {"shard": "albania-latest.parquet", "status": "partial"})

    assert "albania-latest.parquet" in checkpointed_shards(tmp_path)


def test_an_unreadable_checkpoint_forces_the_cli_to_be_asked(tmp_path: Path) -> None:
    """Unreadable is not absent: never treat it as an unstarted shard."""
    _write_checkpoint(tmp_path, "aaa", "{not json")

    assert _UNREADABLE_CHECKPOINT in checkpointed_shards(tmp_path)


def test_a_checkpoint_without_a_shard_name_contributes_nothing(tmp_path: Path) -> None:
    _write_checkpoint(tmp_path, "aaa", {"status": "complete"})

    assert checkpointed_shards(tmp_path) == frozenset()


def _intent(**overrides: object) -> str:
    payload = {
        "outcome": "submitted",
        "result_acknowledged": False,
        "job_id": 1234,
        "shard": "angola-latest.parquet",
    }
    payload.update(overrides)
    return json.dumps(payload)


def _stub_ssh(monkeypatch: pytest.MonkeyPatch, stdout: str) -> None:
    class _Completed:
        def __init__(self) -> None:
            self.stdout = stdout
            self.returncode = 0

    monkeypatch.setattr(
        "osm_polygon_description_tag.workflow.grid_driver.subprocess.run",
        lambda *a, **k: _Completed(),
    )


def test_an_unacknowledged_submission_is_resumed_not_restaged(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Re-staging would overwrite the checkpoint the finished job committed."""
    _stub_ssh(monkeypatch, _intent())

    assert awaiting_collection(_remote(), "angola-latest.parquet") is True


def test_an_acknowledged_submission_is_not_resumed(monkeypatch: pytest.MonkeyPatch) -> None:
    _stub_ssh(monkeypatch, _intent(result_acknowledged=True))

    assert awaiting_collection(_remote(), "angola-latest.parquet") is False


def test_a_rejected_submission_is_not_resumed(monkeypatch: pytest.MonkeyPatch) -> None:
    """A rejected submission produced no results, so there is nothing to collect."""
    _stub_ssh(monkeypatch, _intent(outcome="rejected", job_id=None))

    assert awaiting_collection(_remote(), "angola-latest.parquet") is False


def test_no_recorded_submission_is_not_resumed(monkeypatch: pytest.MonkeyPatch) -> None:
    _stub_ssh(monkeypatch, "")

    assert awaiting_collection(_remote(), "angola-latest.parquet") is False


def test_several_recorded_attempts_are_never_resumed_automatically(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """More than one attempt is ambiguous; an operator must look at it."""
    _stub_ssh(monkeypatch, _intent() + "\n" + _intent(job_id=5678))

    assert awaiting_collection(_remote(), "angola-latest.parquet") is False


def test_unparseable_intent_is_not_resumed(monkeypatch: pytest.MonkeyPatch) -> None:
    _stub_ssh(monkeypatch, "{not json")

    assert awaiting_collection(_remote(), "angola-latest.parquet") is False


# --- exact process contracts -------------------------------------------------

CLI = str(Path(sys.executable).parent / "osm-polygon-description-tag")
RSYNC = ["rsync", "--archive", "--checksum", "--protect-args", "-e", "ssh", "--"]


class _Shell:
    """Record every command the driver runs and answer from a script."""

    def __init__(self, *replies: tuple[int, str, str]) -> None:
        self.replies = list(replies)
        self.calls: list[tuple[object, dict[str, object]]] = []

    def __call__(self, argv: object, **kwargs: object) -> Any:
        self.calls.append((argv, kwargs))
        code, stdout, stderr = self.replies.pop(0) if self.replies else (0, "{}", "")
        return type("Completed", (), {"returncode": code, "stdout": stdout, "stderr": stderr})()

    @property
    def argvs(self) -> list[object]:
        return [argv for argv, _ in self.calls]


def _shell(monkeypatch: pytest.MonkeyPatch, *replies: tuple[int, str, str]) -> _Shell:
    shell = _Shell(*replies)
    monkeypatch.setattr(driver.subprocess, "run", shell)
    return shell


def _logged(capsys: pytest.CaptureFixture[str]) -> list[dict[str, Any]]:
    return [json.loads(line) for line in capsys.readouterr().out.splitlines()]


@pytest.fixture(autouse=True)
def _fixed_clock(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(driver, "utc_now_iso", lambda: "T")


def test_run_returns_stdout_and_passes_a_list_with_exact_options(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    shell = _shell(monkeypatch, (0, "out", ""))

    assert driver._run(("a", "b")) == "out"
    assert shell.calls == [(["a", "b"], {"capture_output": True, "text": True, "check": False})]


def test_run_raises_with_the_code_command_and_stderr(monkeypatch: pytest.MonkeyPatch) -> None:
    _shell(monkeypatch, (3, "", "  broken \n"))

    with pytest.raises(DriverError) as caught:
        driver._run(["a", "b"])

    assert str(caught.value) == "command failed (3): a b\nbroken"


def test_run_json_parses_stdout(monkeypatch: pytest.MonkeyPatch) -> None:
    _shell(monkeypatch, (0, '{"k": 1}', ""))

    assert driver._run_json(["a"]) == {"k": 1}


def test_run_json_refuses_non_json(monkeypatch: pytest.MonkeyPatch) -> None:
    _shell(monkeypatch, (0, "not json", ""))

    with pytest.raises(DriverError) as caught:
        driver._run_json(["a", "b"])

    assert str(caught.value) == "command did not emit JSON: a b"
    assert isinstance(caught.value.__cause__, json.JSONDecodeError)


def test_ssh_helpers_target_the_remote_host(monkeypatch: pytest.MonkeyPatch) -> None:
    shell = _shell(monkeypatch, (0, "plain", ""), (0, '{"a": 2}', ""))

    assert driver._ssh(_remote(), "ls") == "plain"
    assert driver._ssh_json(_remote(), "cat") == {"a": 2}
    assert shell.argvs == [["ssh", "nancy", "ls"], ["ssh", "nancy", "cat"]]


def test_log_writes_one_sorted_json_line(capsys: pytest.CaptureFixture[str]) -> None:
    driver._log("evt", b=1, a="x")

    assert capsys.readouterr().out == '{"a": "x", "at": "T", "b": 1, "event": "evt"}\n'


def test_bundle_dir_joins_the_root_and_the_slug_once() -> None:
    remote = Remote("h", "/root/", "g", "s")

    assert remote.bundle_dir("a b.parquet") == "/root/a-b-parquet"


def test_options_build_the_remote_and_carry_the_documented_defaults() -> None:
    options = _args()

    assert options.remote() == _remote()
    assert (options.project_root, options.ssh_host, options.site) == (Path(), "nancy", "nancy")
    assert (options.poll_seconds, options.job_timeout_seconds) == (20, 2400)
    assert (options.max_shards, options.queue) == (0, None)


@pytest.mark.parametrize(("report", "expected"), [({"complete": True}, True), ({}, False)])
def test_shard_is_complete_asks_the_cli(
    monkeypatch: pytest.MonkeyPatch, report: dict[str, bool], expected: bool
) -> None:
    shell = _shell(monkeypatch, (0, json.dumps(report), ""))

    assert driver.shard_is_complete(Path("/run"), "a.parquet") is expected
    assert shell.argvs == [
        [CLI, "language", "validate", "--run-dir", "/run", "--shard", "a.parquet"]
    ]


def _stage_plan() -> dict[str, Any]:
    return {"payload_dir": "/local/payload", "bundle_id": "b1", "input_row_count": 7, **_plan()}


def test_stage_builds_locally_then_transfers_over_ssh(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    shell = _shell(monkeypatch, (0, json.dumps(_stage_plan()), ""), (0, "", ""), (0, "", ""))

    assert driver.stage(_args(), _remote(), "a b.parquet") == _stage_plan()

    bundle = "/home/op/bundles/a-b-parquet"
    assert shell.argvs == [
        [
            CLI,
            "language",
            "grid",
            "stage",
            "--run-dir",
            "/run",
            "--shard",
            "a b.parquet",
            "--project-root",
            ".",
            "--source-root",
            "/source",
            "--remote-bundle-dir",
            bundle,
            "--processing-seconds",
            "1200",
            "--batch-size",
            "512",
            "--walltime-seconds",
            "1800",
            "--glotlid-model-path",
            "/home/op/models/glotlid-v3/model_v3.bin",
            "--sat-model-path",
            "/home/op/models/sat-3l-sm/model.safetensors",
        ],
        ["ssh", "nancy", f"mkdir -p {bundle}"],
        [*RSYNC, "/local/payload/", f"nancy:{bundle}/"],
    ]
    assert _logged(capsys) == [
        {"event": "staged", "at": "T", "shard": "a b.parquet", "bundle_id": "b1", "rows": 7}
    ]


def test_submit_sends_the_exact_command(monkeypatch: pytest.MonkeyPatch) -> None:
    sent: list[str] = []

    def fake_ssh(remote: Remote, command: str) -> dict[str, Any]:
        assert remote == _remote()
        sent.append(command)
        return _apply_payload("SUBMITTED")

    monkeypatch.setattr(driver, "_ssh_json", fake_ssh)

    submit(_args(queue="q", allow_daytime=True), _remote(), "r.parquet", _plan())

    assert sent == [
        "cd /home/op/project && /home/op/.venv/bin/osm-polygon-description-tag "
        "language grid submit --run-dir /home/op/bundles/x/run --shard r.parquet "
        "--remote-project-dir /home/op/bundles/x/project "
        "--remote-source-dir /home/op/bundles/x/source "
        "--remote-run-dir /home/op/bundles/x/run --site nancy --walltime-seconds 1800 "
        "--processing-seconds 1200 --batch-size 512 "
        "--glotlid-model-path /home/op/models/glotlid-v3/model_v3.bin "
        "--sat-model-path /home/op/models/sat-3l-sm/model.safetensors "
        "--queue q --allow-daytime --apply"
    ]


def test_a_refused_submission_names_the_shard_and_the_payload(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(driver, "_ssh_json", lambda *_a: {"result": None, "b": 1})
    # Keys out of order on purpose: the message must sort them.

    with pytest.raises(DriverError) as caught:
        submit(_args(), _remote(), "r.parquet", _plan())

    assert str(caught.value) == (
        "shard r.parquet was not submitted cleanly; reconcile it by hand and do not "
        'resubmit: {"b": 1, "result": null}'
    )


def test_a_submission_record_without_an_outcome_halts(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(driver, "_ssh_json", lambda *_a: {"result": {"job_id": 1}})

    with pytest.raises(DriverError):
        submit(_args(), _remote(), "r.parquet", _plan())


def test_the_submitted_log_line_is_exact(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setattr(driver, "_ssh_json", lambda *_a: _apply_payload("submitted"))

    submit(_args(), _remote(), "r.parquet", _plan())

    assert _logged(capsys) == [
        {"event": "submitted", "at": "T", "shard": "r.parquet", "job_id": 6923144,
         "outcome": "submitted"}
    ]  # fmt: skip


class _Clock:
    def __init__(self, *readings: float) -> None:
        self.readings = list(readings)

    def __call__(self) -> float:
        return self.readings.pop(0)


def _status(monkeypatch: pytest.MonkeyPatch, *states: dict[str, Any]) -> list[str]:
    sent: list[str] = []
    replies = list(states)

    def fake_ssh(remote: Remote, command: str) -> dict[str, Any]:
        assert remote == _remote()
        sent.append(command)
        return replies.pop(0)

    monkeypatch.setattr(driver, "_ssh_json", fake_ssh)
    return sent


def test_await_terminal_polls_until_terminated(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    sent = _status(monkeypatch, {"state": "Running"}, {"state": "TERMINATED", "detail": "ok"})
    sleeps: list[float] = []
    monkeypatch.setattr(driver.time, "sleep", sleeps.append)
    monkeypatch.setattr(driver.time, "monotonic", _Clock(100.0, 2500.0))

    driver.await_terminal(_args(poll_seconds=7), _remote(), "r.parquet", "/remote/run")

    command = (
        "cd /home/op/project && /home/op/.venv/bin/osm-polygon-description-tag language grid "
        "status --run-dir /remote/run --shard r.parquet --apply"
    )
    assert sent == [command, command]
    assert sleeps == [7]
    assert _logged(capsys) == [
        {"event": "terminal", "at": "T", "shard": "r.parquet", "state": "terminated",
         "detail": "ok"}
    ]  # fmt: skip


def test_await_terminal_gives_up_after_the_job_timeout(monkeypatch: pytest.MonkeyPatch) -> None:
    _status(monkeypatch, {"state": "running"})
    monkeypatch.setattr(driver.time, "sleep", lambda _s: None)
    monkeypatch.setattr(driver.time, "monotonic", _Clock(100.0, 110.5))

    with pytest.raises(DriverError) as caught:
        driver.await_terminal(_args(job_timeout_seconds=10), _remote(), "r.parquet", "/run")

    assert str(caught.value) == (
        "shard r.parquet did not reach a terminal state within 10s; it is still 'running'"
    )


def test_await_terminal_keeps_waiting_exactly_at_the_deadline(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _status(monkeypatch, {"state": "waiting"}, {"state": "terminated"})
    monkeypatch.setattr(driver.time, "sleep", lambda _s: None)
    monkeypatch.setattr(driver.time, "monotonic", _Clock(100.0, 110.0))

    driver.await_terminal(_args(job_timeout_seconds=10), _remote(), "r.parquet", "/run")


@pytest.mark.parametrize("state", ["unknown", "Ambiguous"])
def test_an_unresolved_state_stops_the_run(monkeypatch: pytest.MonkeyPatch, state: str) -> None:
    _status(monkeypatch, {"state": state, "a": 1})
    monkeypatch.setattr(driver.time, "monotonic", _Clock(0.0))

    with pytest.raises(DriverError) as caught:
        driver.await_terminal(_args(), _remote(), "r.parquet", "/run")

    assert str(caught.value) == (
        f"shard r.parquet reconciled to {state.lower()!r}; inspect it before doing anything "
        f'else: {{"a": 1, "state": "{state}"}}'
    )


def test_a_status_without_a_state_times_out_as_none(monkeypatch: pytest.MonkeyPatch) -> None:
    _status(monkeypatch, {})
    monkeypatch.setattr(driver.time, "monotonic", _Clock(0.0, 11.0))

    with pytest.raises(DriverError, match="it is still 'none'$"):
        driver.await_terminal(_args(job_timeout_seconds=10), _remote(), "r.parquet", "/run")


def test_a_status_without_a_state_keeps_polling(monkeypatch: pytest.MonkeyPatch) -> None:
    _status(monkeypatch, {}, {"state": "terminated"})
    monkeypatch.setattr(driver.time, "sleep", lambda _s: None)
    monkeypatch.setattr(driver.time, "monotonic", _Clock(0.0, 1.0))

    driver.await_terminal(_args(), _remote(), "r.parquet", "/run")


def test_collect_retrieves_then_imports(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    shell = _shell(monkeypatch, (0, "", ""), (0, '{"annotation_count": 5}', ""))

    driver.collect(
        _args(retrieval_dir=tmp_path / "retrieve"),
        _remote(),
        "a b.parquet",
        {"remote_run_dir": "/remote/runX//"},
    )

    staging = tmp_path / "retrieve" / "a-b-parquet"
    assert staging.is_dir()
    assert shell.argvs == [
        [*RSYNC, "nancy:/remote/runX/", f"{staging}/"],
        [
            CLI,
            "language",
            "grid",
            "collect",
            "--run-dir",
            "/run",
            "--shard",
            "a b.parquet",
            "--retrieved-run-dir",
            str(staging),
            "--apply",
        ],
    ]
    assert _logged(capsys) == [
        {"event": "collected", "at": "T", "shard": "a b.parquet", "annotations": 5}
    ]


def test_collect_reuses_an_existing_staging_directory(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    (tmp_path / "r-parquet").mkdir()
    _shell(monkeypatch, (0, "", ""), (0, "{}", ""))

    driver.collect(_args(retrieval_dir=tmp_path), _remote(), "r.parquet", _plan())


def test_awaiting_collection_reads_the_intents_on_the_site(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    shell = _shell(monkeypatch, (0, "\n  \n" + _intent() + "\n", ""))

    assert awaiting_collection(_remote(), "a b.parquet") is True
    assert shell.calls == [
        (
            [
                "ssh",
                "nancy",
                "cat /home/op/bundles/a-b-parquet/run/jobs/*/submission-intent.json 2>/dev/null",
            ],
            {"capture_output": True, "text": True, "check": False},
        )
    ]


def test_an_intent_without_the_acknowledged_flag_is_resumed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _stub_ssh(monkeypatch, json.dumps({"outcome": "submitted"}))

    assert awaiting_collection(_remote(), "a.parquet") is True


def test_process_resumes_an_uncollected_shard_from_the_site(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    calls: list[tuple[str, tuple[object, ...]]] = []
    monkeypatch.setattr(
        driver, "awaiting_collection", lambda *a: calls.append(("awaiting", a)) or True
    )
    for name in ("stage", "submit", "await_terminal", "collect"):
        monkeypatch.setattr(driver, name, lambda *a, _n=name: calls.append((_n, a)))

    options, remote = _args(), _remote()
    driver.process(options, remote, "a.parquet")

    run = "/home/op/bundles/a-parquet/run"
    assert calls == [
        ("awaiting", (remote, "a.parquet")),
        ("await_terminal", (options, remote, "a.parquet", run)),
        ("collect", (options, remote, "a.parquet", {"remote_run_dir": run})),
    ]
    assert _logged(capsys) == [{"event": "resuming_uncollected", "at": "T", "shard": "a.parquet"}]


def test_process_stages_submits_waits_and_collects_a_new_shard(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[tuple[str, tuple[object, ...]]] = []
    monkeypatch.setattr(
        driver, "awaiting_collection", lambda *a: calls.append(("awaiting", a)) and False
    )

    def stage(*a: object) -> dict[str, Any]:
        calls.append(("stage", a))
        return _plan()

    monkeypatch.setattr(driver, "stage", stage)
    for name in ("submit", "await_terminal", "collect"):
        monkeypatch.setattr(driver, name, lambda *a, _n=name: calls.append((_n, a)))

    options, remote = _args(), _remote()
    driver.process(options, remote, "a.parquet")

    assert calls == [
        ("awaiting", (remote, "a.parquet")),
        ("stage", (options, remote, "a.parquet")),
        ("submit", (options, remote, "a.parquet", _plan())),
        ("await_terminal", (options, remote, "a.parquet", "/home/op/bundles/x/run")),
        ("collect", (options, remote, "a.parquet", _plan())),
    ]


def _snapshot(run_dir: Path, *shards: tuple[str, int]) -> None:
    run_dir.mkdir(parents=True, exist_ok=True)
    (run_dir / "snapshot.json").write_text(
        json.dumps({"source_files": [{"relative_path": s, "row_count": n} for s, n in shards]}),
        encoding="utf-8",
    )


def _drive(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    *,
    complete: set[str] = frozenset(),  # type: ignore[assignment]
    halt_on: str | None = None,
    **options: Any,
) -> tuple[int, list[str], list[str]]:
    asked: list[str] = []
    processed: list[str] = []

    def is_complete(run_dir: Path, shard: str) -> bool:
        assert run_dir == tmp_path
        asked.append(shard)
        return shard in complete

    def process(opts: DriverOptions, remote: Remote, shard: str) -> None:
        assert remote == opts.remote()
        if shard == halt_on:
            raise DriverError("stop here")
        processed.append(shard)

    monkeypatch.setattr(driver, "shard_is_complete", is_complete)
    monkeypatch.setattr(driver, "process", process)
    code = driver.run_driver(_args(run_dir=tmp_path, **options))
    return code, asked, processed


def test_run_driver_processes_every_shard_in_order(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    _snapshot(tmp_path, ("a", 1), ("b", 2), ("c", 3))

    assert _drive(monkeypatch, tmp_path) == (0, [], ["a", "b", "c"])
    assert _logged(capsys) == [
        {"event": "run_start", "at": "T", "shards": 3, "rows": 6, "stride": 1, "index": 0},
        {"event": "run_end", "at": "T", "completed": 3, "already_complete": 0, "halted": 0},
    ]


def test_run_driver_uses_its_partition(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    _snapshot(tmp_path, ("a", 1), ("b", 2), ("c", 3), ("d", 4))

    result = _drive(monkeypatch, tmp_path, shard_stride=2, shard_index=1)

    assert result == (0, [], ["b", "d"])
    assert _logged(capsys)[0] == {
        "event": "run_start", "at": "T", "shards": 2, "rows": 6, "stride": 2, "index": 1
    }  # fmt: skip


def test_run_driver_asks_the_cli_only_about_checkpointed_shards(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    _snapshot(tmp_path, ("a", 1), ("b", 2), ("c", 3))
    _write_checkpoint(tmp_path, "a", {"shard": "a"})
    _write_checkpoint(tmp_path, "b", {"shard": "b"})

    assert _drive(monkeypatch, tmp_path, complete={"a"}) == (0, ["a", "b"], ["b", "c"])
    assert _logged(capsys)[-1] == {
        "event": "run_end", "at": "T", "completed": 2, "already_complete": 1, "halted": 0
    }  # fmt: skip


def test_every_skipped_shard_is_counted(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    _snapshot(tmp_path, ("a", 1), ("b", 2))
    _write_checkpoint(tmp_path, "a", {"shard": "a"})
    _write_checkpoint(tmp_path, "b", {"shard": "b"})

    assert _drive(monkeypatch, tmp_path, complete={"a", "b"}) == (0, ["a", "b"], [])
    assert _logged(capsys)[-1]["already_complete"] == 2


def test_an_unreadable_checkpoint_makes_the_driver_ask_about_every_shard(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    _snapshot(tmp_path, ("a", 1), ("b", 2))
    _write_checkpoint(tmp_path, "x", "{broken")

    assert _drive(monkeypatch, tmp_path, complete={"b"}) == (0, ["a", "b"], ["a"])


def test_the_shard_budget_stops_the_run(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    _snapshot(tmp_path, ("a", 1), ("b", 2), ("c", 3))

    assert _drive(monkeypatch, tmp_path, max_shards=2) == (0, [], ["a", "b"])
    events = _logged(capsys)
    assert events[1] == {"event": "budget_reached", "at": "T", "processed": 2}
    assert events[2]["completed"] == 2


def test_skipped_shards_do_not_count_against_the_budget(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    _snapshot(tmp_path, ("a", 1), ("b", 2), ("c", 3))
    _write_checkpoint(tmp_path, "a", {"shard": "a"})

    assert _drive(monkeypatch, tmp_path, complete={"a"}, max_shards=1) == (0, ["a"], ["b"])


def test_a_halted_shard_stops_the_run_and_fails_it(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    _snapshot(tmp_path, ("a", 1), ("b", 2), ("c", 3))

    assert _drive(monkeypatch, tmp_path, halt_on="b") == (1, [], ["a"])
    events = _logged(capsys)
    assert events[1] == {
        "event": "halted",
        "at": "T",
        "shard": "b",
        "rows": 2,
        "reason": "stop here",
    }
    assert events[2] == {
        "event": "run_end", "at": "T", "completed": 1, "already_complete": 0, "halted": 1
    }  # fmt: skip


# --- the packaged command ------------------------------------------------------

_REQUIRED = [
    "--run-dir", "/run",
    "--source-root", "/source",
    "--retrieval-dir", "/retrieve",
    "--remote-bundle-root", "/home/op/bundles",
    "--remote-glotlid-model-path", "/home/op/models/glotlid-v3/model_v3.bin",
    "--remote-sat-model-path", "/home/op/models/sat-3l-sm/model.safetensors",
    "--remote-operator-dir", "/home/op/project",
    "--remote-cli", "/home/op/.venv/bin/osm-polygon-description-tag",
]  # fmt: skip


def _run_command(
    monkeypatch: pytest.MonkeyPatch, argv: list[str], code: int
) -> tuple[int, list[DriverOptions]]:
    from osm_polygon_description_tag.cli import run

    seen: list[DriverOptions] = []

    def fake_run_driver(options: DriverOptions) -> int:
        seen.append(options)
        return code

    monkeypatch.setattr(driver, "run_driver", fake_run_driver)
    return run(["language", "grid", "run", *argv]), seen


def test_the_packaged_command_passes_the_documented_defaults(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    assert _run_command(monkeypatch, _REQUIRED, 0) == (0, [_args()])


def test_the_packaged_command_forwards_every_option(monkeypatch: pytest.MonkeyPatch) -> None:
    extra = [
        "--project-root", "/proj",
        "--ssh-host", "lyon-fe",
        "--site", "lyon",
        "--walltime-seconds", "900",
        "--processing-seconds", "600",
        "--batch-size", "64",
        "--poll-seconds", "5",
        "--job-timeout-seconds", "60",
        "--max-shards", "3",
        "--queue", "default",
        "--shard-stride", "4",
        "--shard-index", "2",
        "--allow-daytime",
    ]  # fmt: skip
    expected = _args(
        project_root=Path("/proj"), ssh_host="lyon-fe", site="lyon", walltime_seconds=900,
        processing_seconds=600, batch_size=64, poll_seconds=5, job_timeout_seconds=60,
        max_shards=3, queue="default", shard_stride=4, shard_index=2, allow_daytime=True,
    )  # fmt: skip

    assert _run_command(monkeypatch, [*_REQUIRED, *extra], 0) == (0, [expected])


def test_the_packaged_command_exits_1_when_the_driver_halted(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    code, seen = _run_command(monkeypatch, _REQUIRED, 1)

    assert code == 1
    assert len(seen) == 1
