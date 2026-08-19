"""Generate deterministic CRAP complexity reports from coverage and Radon JSON."""

from __future__ import annotations

import argparse
import fnmatch
import json
from dataclasses import dataclass
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


def _normalise_path(path: str) -> str:
    return Path(path).as_posix().removeprefix("./")


def _coverage_for_block(functions: dict[str, Any], name: str, start_line: int) -> float:
    exact = functions.get(name)
    if exact is not None:
        return float(exact["summary"]["percent_covered"])

    candidates = [
        value
        for function_name, value in functions.items()
        if function_name.rsplit(".", 1)[-1] == name
        and int(value.get("start_line", -1)) == start_line
    ]
    if candidates:
        return float(candidates[0]["summary"]["percent_covered"])
    return 0.0


def build_report(coverage: dict[str, Any], radon: dict[str, Any]) -> dict[str, Any]:
    functions: list[FunctionRisk] = []
    coverage_files = coverage.get("files", {})
    for raw_path, blocks in radon.items():
        path = _normalise_path(raw_path)
        file_coverage = coverage_files.get(raw_path) or coverage_files.get(path) or {}
        covered_functions = file_coverage.get("functions", {})
        for block in blocks:
            if block.get("type") not in {"function", "method"}:
                continue
            start_line = int(block["lineno"])
            functions.append(
                FunctionRisk(
                    path=path,
                    name=str(block["name"]),
                    start_line=start_line,
                    end_line=int(block.get("endline", start_line)),
                    complexity=int(block["complexity"]),
                    coverage_percent=_coverage_for_block(
                        covered_functions, str(block["name"]), start_line
                    ),
                )
            )

    functions.sort(key=lambda item: (-item.crap_score, item.path, item.start_line, item.name))
    return {
        "schema_version": REPORT_SCHEMA_VERSION,
        "formula": FORMULA,
        "functions": [item.as_dict() for item in functions],
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
    if violations:
        print(f"CRAP budget failed: scores must be < {args.max_crap_score:g}")
        for identity, score in violations:
            print(f"{identity}: {score:.6f}")
        raise SystemExit(1)
    scope = "selected functions" if args.pattern else "all functions"
    print(f"CRAP budget passed for {scope}: all scores < {args.max_crap_score:g}")


if __name__ == "__main__":
    main()
