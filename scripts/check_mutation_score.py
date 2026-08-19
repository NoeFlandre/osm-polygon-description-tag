"""Enforce a deterministic mutation score from mutmut's exported statistics."""

from __future__ import annotations

import argparse
import fnmatch
import json
from pathlib import Path
from typing import Any

REPORT_SCHEMA_VERSION = 1
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
    -24: "killed",
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
    mutants_root: Path, patterns: list[str], minimum_score: float
) -> dict[str, Any]:
    stats = {"killed": 0, "total": 0}
    for metadata_path in sorted(mutants_root.glob("src/**/*.py.meta")):
        metadata = json.loads(metadata_path.read_text(encoding="utf-8"))
        for mutant_name, exit_code in sorted(metadata.get("exit_code_by_key", {}).items()):
            if not any(fnmatch.fnmatch(mutant_name, pattern) for pattern in patterns):
                continue
            status = STATUS_BY_EXIT_CODE.get(exit_code, "suspicious")
            stats[status] = int(stats.get(status, 0)) + 1
            stats["total"] += 1
    report = build_report(stats, minimum_score)
    report["patterns"] = patterns
    return report


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    source = parser.add_mutually_exclusive_group(required=True)
    source.add_argument("--stats-json", type=Path)
    source.add_argument("--mutants-root", type=Path)
    parser.add_argument("--pattern", action="append", default=[])
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
        if not args.pattern:
            raise SystemExit("--pattern is required with --mutants-root")
        report = build_metadata_report(args.mutants_root, args.pattern, args.minimum_score)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(
        json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    print(
        f"mutation score: {report['mutation_score_percent']:.2f}% "
        f"({report['killed']}/{report['total']}); minimum {args.minimum_score:.2f}%"
    )
    if not report["passed"]:
        raise SystemExit(1)


if __name__ == "__main__":
    main()
