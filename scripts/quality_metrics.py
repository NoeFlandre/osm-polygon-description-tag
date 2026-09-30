"""Generate deterministic CRAP reports and enforce the repository-wide budget."""

from __future__ import annotations

import argparse
import fnmatch
import json
from pathlib import Path
from typing import Any

if __package__:
    from scripts.crap_reports import (
        build_report,
    )
    from scripts.crap_reports import (
        write_markdown_report as _write_markdown,
    )
    from scripts.crap_reports import (
        write_report as _write_json,
    )
else:
    from crap_reports import (
        build_report,
    )
    from crap_reports import (
        write_markdown_report as _write_markdown,
    )
    from crap_reports import (
        write_report as _write_json,
    )


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    subparsers = parser.add_subparsers(dest="command", required=True)
    crap = subparsers.add_parser("crap", help="build a CRAP report")
    crap.add_argument("--coverage-json", type=Path, required=True)
    crap.add_argument("--radon-json", type=Path, required=True)
    crap.add_argument("--output", type=Path, required=True)
    crap.add_argument("--markdown-output", type=Path)
    check = subparsers.add_parser("check", help="enforce a strict CRAP score budget")
    check.add_argument("--report", type=Path, required=True)
    check.add_argument("--max-crap-score", type=float, required=True)
    check.add_argument(
        "--pattern",
        action="append",
        default=[],
        help="optional fnmatch pattern for path::function identities (repeatable)",
    )
    return parser.parse_args()


def _function_violation(
    function: dict[str, Any], max_score: float, patterns: list[str]
) -> tuple[str, float] | None:
    identity = f"{function['path']}::{function['name']}"
    if patterns and not any(fnmatch.fnmatch(identity, pattern) for pattern in patterns):
        return None
    score = float(function["crap_score"])
    if score < max_score:
        return None
    return identity, score


def _crap_budget_violations(
    payload: dict[str, Any], *, max_score: float, patterns: list[str]
) -> list[tuple[str, float]]:
    return sorted(
        violation
        for function in payload.get("functions", [])
        if (violation := _function_violation(function, max_score, patterns)) is not None
    )


def _report_unmatched(payload: dict[str, Any]) -> None:
    unmatched = payload.get("unmatched_coverage", [])
    print(f"{len(payload.get('functions', []))} functions scored, including closures")
    print(f"{len(unmatched)} without a coverage.py match (scored as 0% covered)")
    for identity in unmatched:
        print(f"  {identity}")


def _write_crap_report(args: argparse.Namespace) -> None:
    payload = build_report(
        json.loads(args.coverage_json.read_text(encoding="utf-8")),
        json.loads(args.radon_json.read_text(encoding="utf-8")),
    )
    _write_json(args.output, payload)
    if args.markdown_output is not None:
        _write_markdown(args.markdown_output, payload)


def _report_violations(violations: list[tuple[str, float]], max_score: float) -> None:
    print(f"CRAP budget failed: scores must be < {max_score:g}")
    for identity, score in violations:
        print(f"{identity}: {score:.6f}")
    raise SystemExit(1)


def _check_report(args: argparse.Namespace) -> None:
    payload = json.loads(args.report.read_text(encoding="utf-8"))
    violations = _crap_budget_violations(
        payload, max_score=args.max_crap_score, patterns=args.pattern
    )
    _report_unmatched(payload)
    if violations:
        _report_violations(violations, args.max_crap_score)
    scope = "selected functions" if args.pattern else "all functions"
    print(f"CRAP budget passed for {scope}: all scores < {args.max_crap_score:g}")


def main() -> None:
    args = _parse_args()
    if args.command == "crap":
        _write_crap_report(args)
    else:
        _check_report(args)


if __name__ == "__main__":
    main()
