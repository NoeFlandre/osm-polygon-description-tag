"""Generate deterministic CRAP complexity reports from coverage and Radon JSON."""

from __future__ import annotations

import argparse
import fnmatch
import json
from collections.abc import Iterator
from dataclasses import dataclass
from datetime import date
from pathlib import Path
from typing import Any

FORMULA = "complexity**2*(1-coverage_fraction)**3+complexity"
REPORT_SCHEMA_VERSION = 1


@dataclass(frozen=True)
class FunctionRisk:
    path: str
    name: str
    start_line: int
    end_line: int
    complexity: int
    coverage_percent: float

    @property
    def crap_score(self) -> float:
        coverage_fraction = self.coverage_percent / 100
        score = self.complexity**2 * (1 - coverage_fraction) ** 3 + self.complexity
        return round(score, 6)

    def as_dict(self) -> dict[str, Any]:
        return {
            "path": self.path,
            "name": self.name,
            "start_line": self.start_line,
            "end_line": self.end_line,
            "complexity": self.complexity,
            "coverage_percent": round(self.coverage_percent, 6),
            "crap_score": self.crap_score,
        }


EXPIRED_PREFIX = "CRAP allow-list expired on"


def _normalise_path(path: str) -> str:
    return Path(path).as_posix().removeprefix("./")


def _coverage_for_block(functions: dict[str, Any], name: str, start_line: int) -> float | None:
    """Coverage percent for a qualified block name, or ``None`` when unmatched."""
    exact = functions.get(name)
    if exact is not None:
        return float(exact["summary"]["percent_covered"])

    leaf = name.rsplit(".", 1)[-1]
    candidates = [
        value
        for function_name, value in functions.items()
        if function_name.rsplit(".", 1)[-1] == leaf
        and int(value.get("start_line", -1)) == start_line
    ]
    if candidates:
        return float(candidates[0]["summary"]["percent_covered"])
    return None


def _scored_blocks(blocks: list[dict[str, Any]], parent: str = "") -> Iterator[tuple[str, dict]]:
    """Yield ``(qualified name, block)`` for every function, method and closure.

    Radon lists nested functions under each block's ``closures``; they are
    named like coverage.py does (``outer.inner``, ``Class.method``).
    """
    for block in blocks:
        if block.get("type") == "class":
            yield from _scored_blocks(block.get("methods", []), f"{parent}{block['name']}.")
            continue
        if block.get("type") not in {"function", "method"}:
            continue
        owner = f"{block['classname']}." if block.get("classname") and not parent else parent
        name = f"{owner}{block['name']}"
        yield name, block
        yield from _scored_blocks(block.get("closures", []), f"{name}.")


def _unique_blocks(blocks: list[dict[str, Any]]) -> dict[tuple[str, int], dict]:
    """Scored blocks keyed by ``(name, line)``.

    Radon 6.0 lists each method both at the top level (with ``classname``)
    and under its class's ``methods``; other versions only nest them. Walking
    both and de-duplicating scores every method exactly once either way.
    """
    return {(name, int(block["lineno"])): block for name, block in _scored_blocks(blocks)}


def build_report(coverage: dict[str, Any], radon: dict[str, Any]) -> dict[str, Any]:
    functions: list[FunctionRisk] = []
    unmatched: list[str] = []
    coverage_files = coverage.get("files", {})
    for raw_path, blocks in radon.items():
        path = _normalise_path(raw_path)
        file_coverage = coverage_files.get(raw_path) or coverage_files.get(path) or {}
        covered_functions = file_coverage.get("functions", {})
        for (name, _line), block in _unique_blocks(blocks).items():
            start_line = int(block["lineno"])
            percent = _coverage_for_block(covered_functions, name, start_line)
            if percent is None:
                # Scored as uncovered (the strict choice) and listed, never silent.
                unmatched.append(f"{path}::{name}")
            functions.append(
                FunctionRisk(
                    path=path,
                    name=name,
                    start_line=start_line,
                    end_line=int(block.get("endline", start_line)),
                    complexity=int(block["complexity"]),
                    coverage_percent=0.0 if percent is None else percent,
                )
            )

    functions.sort(key=lambda item: (-item.crap_score, item.path, item.start_line, item.name))
    return {
        "schema_version": REPORT_SCHEMA_VERSION,
        "formula": FORMULA,
        "functions": [item.as_dict() for item in functions],
        "unmatched_coverage": sorted(unmatched),
    }


def _write_json(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )


def _write_markdown(path: Path, payload: dict[str, Any]) -> None:
    lines = [
        "# CRAP risk report",
        "",
        "CRAP = `complexity^2 * (1 - coverage)^3 + complexity`. "
        "Functions are sorted by descending score.",
        "",
        "| Path | Function | Complexity | Coverage | CRAP |",
        "| --- | --- | ---: | ---: | ---: |",
    ]
    for item in payload["functions"]:
        lines.append(
            f"| `{item['path']}` | `{item['name']}` | {item['complexity']} | "
            f"{item['coverage_percent']:.2f}% | {item['crap_score']:.2f} |"
        )
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def _crap_budget_violations(
    payload: dict[str, Any], *, max_score: float, patterns: list[str]
) -> list[tuple[str, float]]:
    violations: list[tuple[str, float]] = []
    for function in payload.get("functions", []):
        identity = f"{function['path']}::{function['name']}"
        if patterns and not any(fnmatch.fnmatch(identity, pattern) for pattern in patterns):
            continue
        score = float(function["crap_score"])
        if score >= max_score:
            violations.append((identity, score))
    return sorted(violations)


def apply_allowlist(
    violations: list[tuple[str, float]], allowlist: dict[str, Any], *, today: date
) -> tuple[list[tuple[str, float]], list[str]]:
    """Drop allow-listed violations; return what still fails and any list problems.

    Once the list expires nothing is excused (a problem that fails the gate).
    Entries whose function is back within budget are returned as warnings.
    """
    expires = date.fromisoformat(allowlist["expires"])
    if today > expires:
        return violations, [f"{EXPIRED_PREFIX} {expires.isoformat()}"]
    allowed = set(allowlist["functions"])
    remaining = [item for item in violations if item[0] not in allowed]
    return remaining, _stale_entries(allowed, violations)


def _stale_entries(allowed: set[str], violations: list[tuple[str, float]]) -> list[str]:
    within_budget = allowed - {identity for identity, _score in violations}
    return [
        f"remove from the CRAP allow-list, now within budget: {name}"
        for name in sorted(within_budget)
    ]


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
    check.add_argument(
        "--allowlist",
        type=Path,
        help="JSON {expires, functions} excusing listed violations until it expires",
    )
    return parser.parse_args()


def main() -> None:
    args = _parse_args()
    if args.command == "crap":
        payload = build_report(
            json.loads(args.coverage_json.read_text(encoding="utf-8")),
            json.loads(args.radon_json.read_text(encoding="utf-8")),
        )
        _write_json(args.output, payload)
        if args.markdown_output is not None:
            _write_markdown(args.markdown_output, payload)
        return

    payload = json.loads(args.report.read_text(encoding="utf-8"))
    violations = _crap_budget_violations(
        payload, max_score=args.max_crap_score, patterns=args.pattern
    )
    _report_unmatched(payload)
    violations, problems = _allowlisted(violations, args.allowlist)
    for problem in problems:
        print(problem)
    if any(problem.startswith(EXPIRED_PREFIX) for problem in problems):
        raise SystemExit(1)
    if violations:
        print(f"CRAP budget failed: scores must be < {args.max_crap_score:g}")
        for identity, score in violations:
            print(f"{identity}: {score:.6f}")
        raise SystemExit(1)
    scope = "selected functions" if args.pattern else "all functions"
    if args.allowlist is not None:
        scope += " not on the allow-list"
    print(f"CRAP budget passed for {scope}: all scores < {args.max_crap_score:g}")


def _allowlisted(
    violations: list[tuple[str, float]], path: Path | None
) -> tuple[list[tuple[str, float]], list[str]]:
    if path is None:
        return violations, []
    allowlist = json.loads(path.read_text(encoding="utf-8"))
    remaining, problems = apply_allowlist(violations, allowlist, today=date.today())
    excused = len(violations) - len(remaining)
    print(f"{excused} over-budget functions excused by {path} until {allowlist['expires']}")
    return remaining, problems


def _report_unmatched(payload: dict[str, Any]) -> None:
    unmatched = payload.get("unmatched_coverage", [])
    print(f"{len(payload.get('functions', []))} functions scored, including closures")
    print(f"{len(unmatched)} without a coverage.py match (scored as 0% covered)")
    for identity in unmatched:
        print(f"  {identity}")


if __name__ == "__main__":
    main()
