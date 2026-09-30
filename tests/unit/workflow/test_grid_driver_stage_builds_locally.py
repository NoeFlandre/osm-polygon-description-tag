"""Contracts of the multi-shard Grid'5000 driver (``language grid run``).

The driver may only sequence the CLI; it must never decide to spend resources.
These tests pin the three properties that make that true: it halts instead of
resubmitting anything ambiguous, it never enables daytime submission on its
own, and it processes the snapshot's shards in the snapshot's own order.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

import osm_polygon_description_tag.workflow.grid_driver as driver
from osm_polygon_description_tag.workflow.grid_driver import (
    DriverError,
    DriverOptions,
    Remote,
    awaiting_collection,
    submit,
)
from tests.helpers.grid_driver import (
    CLI,
)
from tests.helpers.grid_driver import (
    apply_payload as _apply_payload,
)
from tests.helpers.grid_driver import (
    intent as _intent,
)
from tests.helpers.grid_driver import (
    options as _args,
)
from tests.helpers.grid_driver import (
    plan as _plan,
)
from tests.helpers.grid_driver import (
    remote as _remote,
)
from tests.helpers.grid_driver import (
    shell as _shell,
)
from tests.helpers.grid_driver import (
    stub_ssh as _stub_ssh,
)
from tests.helpers.grid_driver import (
    write_checkpoint as _write_checkpoint,
)

RSYNC = ["rsync", "--archive", "--checksum", "--protect-args", "-e", "ssh", "--"]


def _logged(capsys: pytest.CaptureFixture[str]) -> list[dict[str, Any]]:
    return [json.loads(line) for line in capsys.readouterr().out.splitlines()]


@pytest.fixture(autouse=True)
def _fixed_clock(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(driver, "utc_now_iso", lambda: "T")


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


@pytest.mark.parametrize(
    "flags", [["--shard-stride", "0"], ["--shard-stride", "2", "--shard-index", "2"]]
)
def test_an_impossible_partition_is_a_one_line_cli_error(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], flags: list[str]
) -> None:
    from osm_polygon_description_tag.cli import run

    _snapshot(tmp_path, ("a", 1))
    argv = ["language", "grid", "run", *_REQUIRED[2:], "--run-dir", str(tmp_path), *flags]

    assert run(argv) == 1
    err = capsys.readouterr().err
    assert "partition" in err
    assert "Traceback" not in err
