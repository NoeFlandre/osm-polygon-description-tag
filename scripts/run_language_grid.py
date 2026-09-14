"""Drive the bounded Grid'5000 language run one shard at a time.

This script orchestrates the documented per-shard protocol; it contains no
policy of its own. Every decision that could allocate resources, spend quota,
or overwrite committed state stays inside the CLI:

* ``language grid stage`` builds the portable payload and verifies it;
* ``language grid submit --apply`` gathers live policy evidence and submits;
* ``language grid status --apply`` reconciles a recorded submission;
* ``language grid collect --apply`` validates, imports, and acknowledges.

The driver adds exactly three things the CLI deliberately leaves out.

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
through only when the operator sets it here, having confirmed their own
accounting, exactly as the CLI requires.
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
import time
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

DEFAULT_SITE = "nancy"
DEFAULT_POLL_SECONDS = 20
DEFAULT_JOB_TIMEOUT_SECONDS = 2400
TERMINAL_STATES = frozenset({"terminated"})


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


def _shard_slug(shard: str) -> str:
    """Return a remote directory name that cannot contain shell metacharacters."""
    slug = "".join(character if character.isalnum() else "-" for character in shard)
    return slug.strip("-")


def _log(event: str, **fields: Any) -> None:
    payload = {"event": event, "at": time.strftime("%Y-%m-%dT%H:%M:%S%z"), **fields}
    print(json.dumps(payload, sort_keys=True), flush=True)


def _run(argv: Sequence[str], *, capture_json: bool = False) -> dict[str, Any] | None:
    completed = subprocess.run(  # noqa: S603
        list(argv), capture_output=True, text=True, check=False
    )
    if completed.returncode != 0:
        raise DriverError(
            f"command failed ({completed.returncode}): {' '.join(argv)}\n{completed.stderr.strip()}"
        )
    if not capture_json:
        return None
    try:
        return json.loads(completed.stdout)
    except json.JSONDecodeError as error:
        raise DriverError(f"command did not emit JSON: {' '.join(argv)}") from error


def _ssh(remote: Remote, command: str, *, capture_json: bool = False) -> dict[str, Any] | None:
    return _run(["ssh", remote.ssh_host, command], capture_json=capture_json)


def shards_of(run_dir: Path) -> tuple[tuple[str, int], ...]:
    """Return every snapshot shard with its row count, in snapshot order."""
    snapshot = json.loads((run_dir / "snapshot.json").read_text(encoding="utf-8"))
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


def shard_is_complete(run_dir: Path, shard: str) -> bool:
    """Return whether this shard already has a committed complete checkpoint."""
    report = _run(
        [
            *_cli(),
            "language",
            "validate",
            "--run-dir",
            str(run_dir),
            "--shard",
            shard,
        ],
        capture_json=True,
    )
    assert report is not None
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


def stage(args: argparse.Namespace, remote: Remote, shard: str) -> dict[str, Any]:
    """Build and verify the payload locally, then transfer it over SSH."""
    plan = _run(
        [
            *_cli(),
            "language",
            "grid",
            "stage",
            "--run-dir",
            str(args.run_dir),
            "--shard",
            shard,
            "--project-root",
            str(args.project_root),
            "--source-root",
            str(args.source_root),
            "--remote-bundle-dir",
            remote.bundle_dir(shard),
            "--processing-seconds",
            str(args.processing_seconds),
            "--batch-size",
            str(args.batch_size),
            "--walltime-seconds",
            str(args.walltime_seconds),
            "--glotlid-model-path",
            remote.glotlid_model_path,
            "--sat-model-path",
            remote.sat_model_path,
        ],
        capture_json=True,
    )
    assert plan is not None
    payload = plan["payload_dir"]
    destination = remote.bundle_dir(shard)
    _ssh(remote, f"mkdir -p {destination}")
    _run(
        [
            "rsync",
            "--archive",
            "--checksum",
            "--protect-args",
            "-e",
            "ssh",
            "--",
            f"{payload}/",
            f"{remote.ssh_host}:{destination}/",
        ]
    )
    _log("staged", shard=shard, bundle_id=plan["bundle_id"], rows=plan["input_row_count"])
    return plan


def submit(args: argparse.Namespace, remote: Remote, shard: str, plan: dict[str, Any]) -> None:
    """Submit through the CLI on the frontend, behind its own apply gate."""
    command = " ".join(
        [
            f"cd {args.remote_operator_dir} &&",
            f"{args.remote_cli}",
            "language grid submit",
            f"--run-dir {plan['remote_run_dir']}",
            f"--shard {shard}",
            f"--remote-project-dir {plan['remote_project_dir']}",
            f"--remote-source-dir {plan['remote_source_dir']}",
            f"--remote-run-dir {plan['remote_run_dir']}",
            f"--site {args.site}",
            f"--walltime-seconds {args.walltime_seconds}",
            f"--processing-seconds {args.processing_seconds}",
            f"--batch-size {args.batch_size}",
            f"--glotlid-model-path {remote.glotlid_model_path}",
            f"--sat-model-path {remote.sat_model_path}",
            f"--queue {args.queue}" if args.queue else "",
            "--allow-daytime" if args.allow_daytime else "",
            "--apply",
        ]
    )
    payload = _ssh(remote, command, capture_json=True)
    assert payload is not None
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
    if str(record.get("outcome", "")).lower() != "submitted":
        return None
    return record


def await_terminal(args: argparse.Namespace, remote: Remote, shard: str, run_dir: str) -> None:
    """Poll the recorded submission until the scheduler reports a terminal state."""
    deadline = time.monotonic() + args.job_timeout_seconds
    while True:
        command = (
            f"cd {args.remote_operator_dir} && {args.remote_cli} language grid status "
            f"--run-dir {run_dir} --shard {shard} --apply"
        )
        status = _ssh(remote, command, capture_json=True)
        assert status is not None
        state = str(status.get("state", "")).lower()
        if state in TERMINAL_STATES:
            _log("terminal", shard=shard, state=state, detail=status.get("detail"))
            return
        if state in {"unknown", "ambiguous"}:
            raise DriverError(
                f"shard {shard} reconciled to {state!r}; inspect it before doing anything else: "
                f"{json.dumps(status, sort_keys=True)}"
            )
        if time.monotonic() > deadline:
            raise DriverError(
                f"shard {shard} did not reach a terminal state within "
                f"{args.job_timeout_seconds}s; it is still {state!r}"
            )
        time.sleep(args.poll_seconds)


def collect(args: argparse.Namespace, remote: Remote, shard: str, plan: dict[str, Any]) -> None:
    """Retrieve the shard's state over SSH, then import and acknowledge it."""
    staging = args.retrieval_dir / _shard_slug(shard)
    staging.mkdir(parents=True, exist_ok=True)
    remote_run = plan["remote_run_dir"].rstrip("/")
    _run(
        [
            "rsync",
            "--archive",
            "--checksum",
            "--protect-args",
            "-e",
            "ssh",
            "--",
            f"{remote.ssh_host}:{remote_run}/",
            f"{staging}/",
        ]
    )
    report = _run(
        [
            *_cli(),
            "language",
            "grid",
            "collect",
            "--run-dir",
            str(args.run_dir),
            "--shard",
            shard,
            "--retrieved-run-dir",
            str(staging),
            "--apply",
        ],
        capture_json=True,
    )
    assert report is not None
    _log("collected", shard=shard, annotations=report.get("annotation_count"))


def process(args: argparse.Namespace, remote: Remote, shard: str) -> None:
    plan = stage(args, remote, shard)
    submit(args, remote, shard, plan)
    await_terminal(args, remote, shard, plan["remote_run_dir"])
    collect(args, remote, shard, plan)


def main(argv: Sequence[str] | None = None) -> int:
    args = _parse_args(argv)
    remote = Remote(
        args.ssh_host,
        args.remote_bundle_root,
        args.remote_glotlid_model_path,
        args.remote_sat_model_path,
    )
    shards = selected_shards(
        shards_of(args.run_dir), stride=args.shard_stride, index=args.shard_index
    )
    _log(
        "run_start",
        shards=len(shards),
        rows=sum(rows for _, rows in shards),
        stride=args.shard_stride,
        index=args.shard_index,
    )

    done = failed = skipped = 0
    for shard, rows in shards:
        if args.max_shards and done >= args.max_shards:
            _log("budget_reached", processed=done)
            break
        if shard_is_complete(args.run_dir, shard):
            skipped += 1
            continue
        try:
            process(args, remote, shard)
        except DriverError as error:
            failed += 1
            _log("halted", shard=shard, rows=rows, reason=str(error))
            break
        done += 1
    _log("run_end", completed=done, already_complete=skipped, halted=failed)
    return 1 if failed else 0


def _parse_args(argv: Sequence[str] | None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--run-dir", type=Path, required=True)
    parser.add_argument("--project-root", type=Path, default=Path())
    parser.add_argument("--source-root", type=Path, required=True)
    parser.add_argument("--retrieval-dir", type=Path, required=True)
    parser.add_argument("--ssh-host", default=DEFAULT_SITE)
    parser.add_argument("--site", default=DEFAULT_SITE)
    parser.add_argument("--remote-bundle-root", required=True)
    parser.add_argument("--remote-glotlid-model-path", required=True)
    parser.add_argument("--remote-sat-model-path", required=True)
    parser.add_argument("--remote-operator-dir", required=True)
    parser.add_argument("--remote-cli", required=True)
    parser.add_argument("--walltime-seconds", type=int, default=1800)
    parser.add_argument("--processing-seconds", type=int, default=1200)
    parser.add_argument("--batch-size", type=int, default=512)
    parser.add_argument("--poll-seconds", type=int, default=DEFAULT_POLL_SECONDS)
    parser.add_argument("--job-timeout-seconds", type=int, default=DEFAULT_JOB_TIMEOUT_SECONDS)
    parser.add_argument("--max-shards", type=int, default=0)
    parser.add_argument(
        "--queue",
        default=None,
        help=(
            "Scheduler queue to request; sites disagree on what a bare "
            "submission means, and several reject the queue they pick themselves."
        ),
    )
    parser.add_argument(
        "--shard-stride",
        type=int,
        default=1,
        help="Split the snapshot across this many cooperating drivers.",
    )
    parser.add_argument(
        "--shard-index",
        type=int,
        default=0,
        help="Which share of --shard-stride this driver owns.",
    )
    parser.add_argument(
        "--allow-daytime",
        action="store_true",
        help="Only set this once you have confirmed your own daytime accounting.",
    )
    return parser.parse_args(argv)


if __name__ == "__main__":
    sys.exit(main())
