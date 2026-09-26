"""Enforce a deterministic mutation score from mutmut's exported statistics."""

from __future__ import annotations

import argparse
import fnmatch
import json
from collections.abc import Iterator
from pathlib import Path
from typing import Any

REPORT_SCHEMA_VERSION = 2
STATUS_KEYS = (
    "survived",
    "no_tests",
    "skipped",
    "suspicious",
    "timeout",
    "check_was_interrupted_by_user",
    "caught_by_type_check",
    "segfault",
)
STATUS_BY_EXIT_CODE = {
    None: "not_checked",
    0: "survived",
    -24: "timeout",
    1: "killed",
    2: "check_was_interrupted_by_user",
    3: "killed",
    5: "no_tests",
    24: "timeout",
    33: "no_tests",
    34: "skipped",
    35: "suspicious",
    36: "timeout",
    37: "caught_by_type_check",
    152: "timeout",
    255: "timeout",
}


def scoped_metadata_paths(mutants_root: Path, scope_file: Path | None) -> list[Path]:
    """Return the mutmut result files to score.

    A sharded run only executes the mutants of the sources in its shard, so it
    must score exactly those. Scoring the whole tree would count another
    shard's untouched mutants as unkilled.
    """
    if scope_file is None:
        return sorted(mutants_root.glob("src/**/*.py.meta"))
    sources = [
        line.strip() for line in scope_file.read_text(encoding="utf-8").splitlines() if line.strip()
    ]
    paths = [mutants_root / f"{source}.meta" for source in sources]
    return sorted(path for path in paths if path.is_file())


def iter_mutant_exit_codes(
    mutants_root: Path, scope_file: Path | None = None
) -> Iterator[tuple[str, Any]]:
    """Yield ``(mutant_name, exit_code)`` pairs, file by file, in sorted order."""
    for metadata_path in scoped_metadata_paths(mutants_root, scope_file):
        metadata = json.loads(metadata_path.read_text(encoding="utf-8"))
        yield from sorted(metadata.get("exit_code_by_key", {}).items())


def write_json_report(path: Path, payload: dict[str, Any]) -> None:
    """Write ``payload`` as stable, pretty, UTF-8 JSON, creating parent dirs."""
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )


def build_report(stats: dict[str, Any], minimum_score: float) -> dict[str, Any]:
    total = int(stats.get("total", 0))
    killed = int(stats.get("killed", 0))
    if total <= 0:
        raise ValueError("mutmut reported no mutants")
    score = round(killed / total * 100, 6)
    return {
        "schema_version": REPORT_SCHEMA_VERSION,
        "minimum_score_percent": minimum_score,
        "mutation_score_percent": score,
        "killed": killed,
        "total": total,
        "unresolved": {key: int(stats.get(key, 0)) for key in STATUS_KEYS},
        "passed": score >= minimum_score
        and all(int(stats.get(key, 0)) == 0 for key in STATUS_KEYS),
    }


def build_metadata_report(
    mutants_root: Path,
    patterns: list[str],
    minimum_score: float,
    scope_file: Path | None = None,
) -> dict[str, Any]:
    stats = {"killed": 0, "total": 0}
    unresolved: dict[str, list[str]] = {}
    for mutant_name, exit_code in iter_mutant_exit_codes(mutants_root, scope_file):
        if patterns and not any(fnmatch.fnmatch(mutant_name, pattern) for pattern in patterns):
            continue
        status = STATUS_BY_EXIT_CODE.get(exit_code, "suspicious")
        stats[status] = int(stats.get(status, 0)) + 1
        stats["total"] += 1
        if status in STATUS_KEYS:
            unresolved.setdefault(status, []).append(mutant_name)
    report = build_report(stats, minimum_score)
    report["patterns"] = patterns
    # Name what survived. The score alone says a gate failed but not which
    # mutant to go and kill, which left the only actionable list buried in
    # per-shard CI state that is discarded when the runner is torn down.
    report["unresolved_mutants"] = {status: sorted(names) for status, names in unresolved.items()}
    return report


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    source = parser.add_mutually_exclusive_group(required=True)
    source.add_argument("--stats-json", type=Path)
    source.add_argument("--mutants-root", type=Path)
    parser.add_argument("--pattern", action="append", default=[])
    parser.add_argument(
        "--scope-file",
        type=Path,
        metavar="FILE",
        help="newline-delimited source paths; only their mutants are scored",
    )
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--minimum-score", type=float, default=90.0)
    return parser.parse_args()


def main() -> None:
    args = _parse_args()
    if args.stats_json is not None:
        report = build_report(
            json.loads(args.stats_json.read_text(encoding="utf-8")), args.minimum_score
        )
    else:
        report = build_metadata_report(
            args.mutants_root,
            args.pattern,
            args.minimum_score,
            scope_file=args.scope_file,
        )
    write_json_report(args.output, report)
    print(
        f"mutation score: {report['mutation_score_percent']:.2f}% "
        f"({report['killed']}/{report['total']}); minimum {args.minimum_score:.2f}%"
    )
    if not report["passed"]:
        raise SystemExit(1)


if __name__ == "__main__":
    main()
