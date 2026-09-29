"""Drive the bounded Grid'5000 language run one shard at a time.

``language grid run`` orchestrates the documented per-shard protocol; it
contains no policy of its own. Every decision that could allocate resources,
spend quota, or overwrite committed state stays inside the other CLI commands:

* ``language grid stage`` builds the portable payload and verifies it;
* ``language grid submit --apply`` gathers live policy evidence and submits;
* ``language grid status --apply`` reconciles a recorded submission;
* ``language grid collect --apply`` validates, imports, and acknowledges.

The driver adds exactly three things those commands deliberately leave out.

**The transfer.** ``grid stage`` emits a *filesystem* rsync argv, so it cannot
reach the site from a laptop. The transfer is arranged here over SSH, which is
the separate authorised transfer the runbook describes.

**Sequencing.** One shard is in flight at a time, in the snapshot's own
deterministic order, and the next shard starts only after the previous one is
collected and acknowledged.

**Stopping.** Anything the protocol calls ambiguous, active, or unexpected ends
the run. A job that may already exist is never submitted again; it is left for
an operator to reconcile. Re-running the driver resumes from the on-disk
checkpoints, so a stopped run loses no completed shard.

Daytime submission is never enabled implicitly: ``--allow-daytime`` is passed
through only when the operator sets it, having confirmed their own
accounting, exactly as ``grid submit`` requires.
"""

from __future__ import annotations

import json
import subprocess
import sys
import time
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from osm_polygon_description_tag.dataset.languages.worker import DEFAULT_BATCH_SIZE
from osm_polygon_description_tag.runtime.time import utc_now_iso
from osm_polygon_description_tag.workflow.grid_policy import (
    MAX_PROCESSING_SECONDS,
    MAX_WALLTIME_SECONDS,
)

DEFAULT_SITE = "nancy"
DEFAULT_POLL_SECONDS = 20
DEFAULT_JOB_TIMEOUT_SECONDS = 2400
TERMINAL_STATES = frozenset({"terminated"})
_UNRESOLVED_STATES = frozenset({"unknown", "ambiguous"})
_RSYNC = ("rsync", "--archive", "--checksum", "--protect-args", "-e", "ssh", "--")
# Sentinel: a checkpoint we could not read, which forces the CLI to be asked
# about every shard rather than assuming an unreadable one means "unstarted".
_UNREADABLE_CHECKPOINT = "\x00unreadable"


class DriverError(RuntimeError):
    """Raised when the run must stop and an operator has to look at it."""


@dataclass(frozen=True)
class Remote:
    """Where the site is, and where this run's files live on it."""

    ssh_host: str
    bundle_root: str
    glotlid_model_path: str
    sat_model_path: str

    def bundle_dir(self, shard: str) -> str:
        return f"{self.bundle_root.rstrip('/')}/{_shard_slug(shard)}"


@dataclass(frozen=True)
class DriverOptions:
    """Everything one ``language grid run`` invocation was asked to do."""

    run_dir: Path
    source_root: Path
    retrieval_dir: Path
    remote_bundle_root: str
    remote_glotlid_model_path: str
    remote_sat_model_path: str
    remote_operator_dir: str
    remote_cli: str
    project_root: Path = Path()
    ssh_host: str = DEFAULT_SITE
    site: str = DEFAULT_SITE
    walltime_seconds: int = MAX_WALLTIME_SECONDS
    processing_seconds: int = MAX_PROCESSING_SECONDS
    batch_size: int = DEFAULT_BATCH_SIZE
    poll_seconds: int = DEFAULT_POLL_SECONDS
    job_timeout_seconds: int = DEFAULT_JOB_TIMEOUT_SECONDS
    max_shards: int = 0
    queue: str | None = None
    shard_stride: int = 1
    shard_index: int = 0
    allow_daytime: bool = False

    def remote(self) -> Remote:
        return Remote(
            self.ssh_host,
            self.remote_bundle_root,
            self.remote_glotlid_model_path,
            self.remote_sat_model_path,
        )


@dataclass
class _Tally:
    done: int = 0
    skipped: int = 0
    halted: bool = False


def _shard_slug(shard: str) -> str:
    """Return a remote directory name that cannot contain shell metacharacters."""
    slug = "".join(character if character.isalnum() else "-" for character in shard)
    return slug.strip("-")


def _log(event: str, **fields: Any) -> None:
    payload = {"event": event, "at": utc_now_iso(), **fields}
    sys.stdout.write(json.dumps(payload, sort_keys=True) + "\n")
    sys.stdout.flush()


def _run(argv: Sequence[str]) -> str:
    """Run one command and return its stdout; any failure stops the run."""
    completed = subprocess.run(  # noqa: S603 - argv lists built by this module
        list(argv), capture_output=True, text=True, check=False
    )
    if completed.returncode != 0:
        raise DriverError(
            f"command failed ({completed.returncode}): {' '.join(argv)}\n{completed.stderr.strip()}"
        )
    return completed.stdout


def _run_json(argv: Sequence[str]) -> dict[str, Any]:
    stdout = _run(argv)
    try:
        return json.loads(stdout)
    except json.JSONDecodeError as error:
        raise DriverError(f"command did not emit JSON: {' '.join(argv)}") from error


def _ssh(remote: Remote, command: str) -> str:
    return _run(["ssh", remote.ssh_host, command])


def _ssh_json(remote: Remote, command: str) -> dict[str, Any]:
    return _run_json(["ssh", remote.ssh_host, command])


def shards_of(run_dir: Path) -> tuple[tuple[str, int], ...]:
    """Return every snapshot shard with its row count, in snapshot order."""
    snapshot = json.loads((run_dir / "snapshot.json").read_bytes())
    return tuple(
        (entry["relative_path"], int(entry["row_count"])) for entry in snapshot["source_files"]
    )


def selected_shards(
    shards: tuple[tuple[str, int], ...], *, stride: int, index: int
) -> tuple[tuple[str, int], ...]:
    """Return this driver's share of the snapshot, in snapshot order.

    Several drivers may work one snapshot at once, each against its own run
    directory at its own site, because the run-wide locks make a shared run
    directory single-writer by design. Round-robin by position keeps the shares
    disjoint and complete without any coordination between them: every shard
    belongs to exactly one index, so no shard is processed twice and none is
    dropped.
    """
    if stride < 1:
        raise DriverError(f"partition stride must be at least 1, not {stride}")
    if not 0 <= index < stride:
        raise DriverError(f"partition index {index} is outside a stride of {stride}")
    return shards[index::stride]


def checkpointed_shards(run_dir: Path) -> frozenset[str]:
    """Return the shard names that have a checkpoint written at all.

    ``shard_is_complete`` spawns a CLI process per shard, and each one pays a
    full interpreter start plus package import before it can answer. A driver
    resuming a 386-shard snapshot therefore paid hundreds of process starts
    just to rediscover that most shards had never been touched -- minutes of
    startup before the first submission, growing with the partition.

    A shard with no checkpoint on disk cannot be complete, so one directory
    scan answers for all of them. This narrows *who gets asked*; it never
    decides completeness, which stays with the CLI.
    """
    shards_root = run_dir / "shards"
    if not shards_root.is_dir():
        return frozenset()
    names = (_checkpoint_shard(path) for path in shards_root.glob("*/checkpoint.json"))
    return frozenset(name for name in names if name is not None)


def _checkpoint_shard(checkpoint: Path) -> str | None:
    try:
        payload = json.loads(checkpoint.read_bytes())
    except (OSError, ValueError):
        # An unreadable checkpoint is not evidence of absence: let the CLI
        # look at this shard rather than silently treating it as unstarted.
        return _UNREADABLE_CHECKPOINT
    shard = payload.get("shard")
    return shard if isinstance(shard, str) else None


def shard_is_complete(run_dir: Path, shard: str) -> bool:
    """Return whether this shard already has a committed complete checkpoint."""
    report = _run_json(
        [*_cli(), "language", "validate", "--run-dir", str(run_dir), "--shard", shard]
    )
    return bool(report.get("complete"))


def _cli() -> list[str]:
    """Return the CLI invocation, deliberately without ``uv``.

    ``uv run`` locks the shared uv cache for the life of the command. This
    driver calls the CLI once per shard just to ask whether it is already
    complete, so going through ``uv`` makes a process that holds that lock
    spawn one that waits for it; several drivers in parallel then wedge
    completely. The console script sits beside the running interpreter, so
    calling it directly is both lock-free and one process cheaper.
    """
    return [str(Path(sys.executable).parent / "osm-polygon-description-tag")]


def stage(options: DriverOptions, remote: Remote, shard: str) -> dict[str, Any]:
    """Build and verify the payload locally, then transfer it over SSH."""
    destination = remote.bundle_dir(shard)
    plan = _run_json(
        [
            *_cli(),
            "language",
            "grid",
            "stage",
            "--run-dir",
            str(options.run_dir),
            "--shard",
            shard,
            "--project-root",
            str(options.project_root),
            "--source-root",
            str(options.source_root),
            "--remote-bundle-dir",
            destination,
            "--processing-seconds",
            str(options.processing_seconds),
            "--batch-size",
            str(options.batch_size),
            "--walltime-seconds",
            str(options.walltime_seconds),
            "--glotlid-model-path",
            remote.glotlid_model_path,
            "--sat-model-path",
            remote.sat_model_path,
        ]
    )
    _ssh(remote, f"mkdir -p {destination}")
    _run([*_RSYNC, f"{plan['payload_dir']}/", f"{remote.ssh_host}:{destination}/"])
    _log("staged", shard=shard, bundle_id=plan["bundle_id"], rows=plan["input_row_count"])
    return plan


def submit(options: DriverOptions, remote: Remote, shard: str, plan: dict[str, Any]) -> None:
    """Submit through the CLI on the frontend, behind its own apply gate."""
    parts = [
        f"cd {options.remote_operator_dir} &&",
        options.remote_cli,
        "language grid submit",
        f"--run-dir {plan['remote_run_dir']}",
        f"--shard {shard}",
        f"--remote-project-dir {plan['remote_project_dir']}",
        f"--remote-source-dir {plan['remote_source_dir']}",
        f"--remote-run-dir {plan['remote_run_dir']}",
        f"--site {options.site}",
        f"--walltime-seconds {options.walltime_seconds}",
        f"--processing-seconds {options.processing_seconds}",
        f"--batch-size {options.batch_size}",
        f"--glotlid-model-path {remote.glotlid_model_path}",
        f"--sat-model-path {remote.sat_model_path}",
    ]
    if options.queue:
        parts.append(f"--queue {options.queue}")
    if options.allow_daytime:
        parts.append("--allow-daytime")
    payload = _ssh_json(remote, " ".join([*parts, "--apply"]))
    submission = _submitted_job(payload)
    if submission is None:
        raise DriverError(
            f"shard {shard} was not submitted cleanly; "
            f"reconcile it by hand and do not resubmit: {json.dumps(payload, sort_keys=True)}"
        )
    _log("submitted", shard=shard, job_id=submission.get("job_id"), outcome="submitted")


def _submitted_job(payload: dict[str, Any]) -> dict[str, Any] | None:
    """Return the submission record only when OAR cleanly accepted a job.

    ``language grid submit --apply`` reports the submission under ``result``,
    which is null whenever its own apply gate refused. Anything else --- a
    missing record, a non-object, or any outcome but ``submitted`` --- means a
    job may or may not exist, and the caller must stop rather than guess.
    """
    record = payload.get("result")
    if not isinstance(record, dict):
        return None
    if str(record.get("outcome")).lower() != "submitted":
        return None
    return record


def await_terminal(options: DriverOptions, remote: Remote, shard: str, run_dir: str) -> None:
    """Poll the recorded submission until the scheduler reports a terminal state."""
    deadline = time.monotonic() + options.job_timeout_seconds
    command = (
        f"cd {options.remote_operator_dir} && {options.remote_cli} language grid status "
        f"--run-dir {run_dir} --shard {shard} --apply"
    )
    while True:
        state = _poll_state(remote, command, shard)
        if state in TERMINAL_STATES:
            return
        if time.monotonic() > deadline:
            raise DriverError(
                f"shard {shard} did not reach a terminal state within "
                f"{options.job_timeout_seconds}s; it is still {state!r}"
            )
        time.sleep(options.poll_seconds)


def _poll_state(remote: Remote, command: str, shard: str) -> str:
    status = _ssh_json(remote, command)
    state = str(status.get("state")).lower()
    if state in TERMINAL_STATES:
        _log("terminal", shard=shard, state=state, detail=status.get("detail"))
    elif state in _UNRESOLVED_STATES:
        raise DriverError(
            f"shard {shard} reconciled to {state!r}; inspect it before doing anything else: "
            f"{json.dumps(status, sort_keys=True)}"
        )
    return state


def collect(options: DriverOptions, remote: Remote, shard: str, plan: dict[str, Any]) -> None:
    """Retrieve the shard's state over SSH, then import and acknowledge it."""
    staging = options.retrieval_dir / _shard_slug(shard)
    staging.mkdir(parents=True, exist_ok=True)
    remote_run = plan["remote_run_dir"].rstrip("/")
    _run([*_RSYNC, f"{remote.ssh_host}:{remote_run}/", f"{staging}/"])
    report = _run_json(
        [
            *_cli(),
            "language",
            "grid",
            "collect",
            "--run-dir",
            str(options.run_dir),
            "--shard",
            shard,
            "--retrieved-run-dir",
            str(staging),
            "--apply",
        ]
    )
    _log("collected", shard=shard, annotations=report.get("annotation_count"))


def awaiting_collection(remote: Remote, shard: str) -> bool:
    """Return whether the site still holds results this run never collected.

    ``grid stage`` rewrites the remote run directory from the local one, so
    staging a shard whose job already finished overwrites the checkpoint that
    job committed. The parts and receipts survive, but nothing records them any
    more, and collection then refuses the shard as "unexpected part file not
    recorded by the checkpoint" -- finished work stranded by a restart.

    A recorded submission that was never acknowledged is exactly that case, so
    the shard is resumed from the site instead of being staged again.
    """
    remote_run = f"{remote.bundle_dir(shard)}/run"
    argv = ["ssh", remote.ssh_host, f"cat {remote_run}/jobs/*/submission-intent.json 2>/dev/null"]
    listing = subprocess.run(  # noqa: S603 - fixed argv
        argv, capture_output=True, text=True, check=False
    )
    intent = _single_intent(listing.stdout)
    return (
        intent is not None
        and intent.get("outcome") == "submitted"
        and not intent.get("result_acknowledged")
    )


def _single_intent(stdout: str) -> dict[str, Any] | None:
    """Return the one recorded intent, or None when there is none or several."""
    intents = [line for line in stdout.splitlines() if line.strip()]
    if len(intents) != 1:
        # Nothing recorded, or more than one attempt: not an unambiguous resume.
        return None
    try:
        return json.loads(intents[0])
    except json.JSONDecodeError:
        return None


def process(options: DriverOptions, remote: Remote, shard: str) -> None:
    if awaiting_collection(remote, shard):
        remote_run = f"{remote.bundle_dir(shard)}/run"
        _log("resuming_uncollected", shard=shard)
        await_terminal(options, remote, shard, remote_run)
        collect(options, remote, shard, {"remote_run_dir": remote_run})
        return
    plan = stage(options, remote, shard)
    submit(options, remote, shard, plan)
    await_terminal(options, remote, shard, plan["remote_run_dir"])
    collect(options, remote, shard, plan)


def run_driver(options: DriverOptions) -> int:
    """Process this driver's shards in order; return 1 if one halted the run."""
    remote = options.remote()
    shards = selected_shards(
        shards_of(options.run_dir), stride=options.shard_stride, index=options.shard_index
    )
    _log(
        "run_start",
        shards=len(shards),
        rows=sum(rows for _, rows in shards),
        stride=options.shard_stride,
        index=options.shard_index,
    )
    tally = _drive(options, remote, shards)
    _log("run_end", completed=tally.done, already_complete=tally.skipped, halted=int(tally.halted))
    return 1 if tally.halted else 0


def _drive(options: DriverOptions, remote: Remote, shards: tuple[tuple[str, int], ...]) -> _Tally:
    tally = _Tally()
    checkpointed = checkpointed_shards(options.run_dir)
    for shard, rows in shards:
        if _budget_reached(options, tally.done):
            break
        if _already_complete(options.run_dir, shard, checkpointed):
            tally.skipped += 1
        elif _processed(options, remote, shard, rows):
            tally.done += 1
        else:
            tally.halted = True
            break
    return tally


def _budget_reached(options: DriverOptions, done: int) -> bool:
    if options.max_shards and done >= options.max_shards:
        _log("budget_reached", processed=done)
        return True
    return False


def _already_complete(run_dir: Path, shard: str, checkpointed: frozenset[str]) -> bool:
    may_be_complete = _UNREADABLE_CHECKPOINT in checkpointed or shard in checkpointed
    return may_be_complete and shard_is_complete(run_dir, shard)


def _processed(options: DriverOptions, remote: Remote, shard: str, rows: int) -> bool:
    try:
        process(options, remote, shard)
    except DriverError as error:
        _log("halted", shard=shard, rows=rows, reason=str(error))
        return False
    return True


__all__ = [
    "DEFAULT_JOB_TIMEOUT_SECONDS",
    "DEFAULT_POLL_SECONDS",
    "DEFAULT_SITE",
    "DriverError",
    "DriverOptions",
    "Remote",
    "run_driver",
]
