"""Build CRAP risk reports from coverage.py and Radon data."""

from __future__ import annotations

from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path
from typing import Any

if __package__:
    from scripts.report_io import write_json_report as _write_json
    from scripts.report_io import write_text_report as _write_text
else:
    from report_io import write_json_report as _write_json
    from report_io import write_text_report as _write_text

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


def _coverage_for_block(functions: dict[str, Any], name: str, start_line: int) -> float | None:
    """Find exact coverage data or fall back to a unique leaf-name line match."""
    exact = functions.get(name)
    if exact is not None:
        return float(exact["summary"]["percent_covered"])
    return _coverage_by_start_line(functions, name.rsplit(".", 1)[-1], start_line)


def _coverage_by_start_line(
    functions: dict[str, Any], leaf_name: str, start_line: int
) -> float | None:
    for function_name, value in functions.items():
        if (
            function_name.rsplit(".", 1)[-1] == leaf_name
            and int(value.get("start_line", -1)) == start_line
        ):
            return float(value["summary"]["percent_covered"])
    return None


def _scored_blocks(blocks: list[dict[str, Any]], parent: str = "") -> Iterator[tuple[str, dict]]:
    """Yield Radon functions, methods, and nested closures with coverage names."""
    for block in blocks:
        yield from _scored_block(block, parent)


def _scored_block(block: dict[str, Any], parent: str) -> Iterator[tuple[str, dict]]:
    block_type = block.get("type")
    if block_type == "class":
        yield from _scored_blocks(block.get("methods", []), f"{parent}{block['name']}.")
        return
    if block_type not in {"function", "method"}:
        return
    owner = f"{block['classname']}." if block.get("classname") and not parent else parent
    name = f"{owner}{block['name']}"
    yield name, block
    yield from _scored_blocks(block.get("closures", []), f"{name}.")


def _unique_blocks(blocks: list[dict[str, Any]]) -> dict[tuple[str, int], dict]:
    """Deduplicate Radon methods that appear both top-level and under classes."""
    return {(name, int(block["lineno"])): block for name, block in _scored_blocks(blocks)}


def _file_risks(
    path: str, blocks: list[dict[str, Any]], covered_functions: dict[str, Any]
) -> tuple[list[FunctionRisk], list[str]]:
    risks: list[FunctionRisk] = []
    unmatched: list[str] = []
    for (name, _line), block in _unique_blocks(blocks).items():
        start_line = int(block["lineno"])
        percent = _coverage_for_block(covered_functions, name, start_line)
        if percent is None:
            unmatched.append(f"{path}::{name}")
        risks.append(
            FunctionRisk(
                path=path,
                name=name,
                start_line=start_line,
                end_line=int(block.get("endline", start_line)),
                complexity=int(block["complexity"]),
                coverage_percent=0.0 if percent is None else percent,
            )
        )
    return risks, unmatched


def build_report(coverage: dict[str, Any], radon: dict[str, Any]) -> dict[str, Any]:
    """Join measured line coverage to every Radon function and closure."""
    functions: list[FunctionRisk] = []
    unmatched: list[str] = []
    coverage_files = coverage.get("files", {})
    for raw_path, blocks in radon.items():
        path = _normalise_path(raw_path)
        file_coverage = coverage_files.get(raw_path) or coverage_files.get(path) or {}
        file_risks, file_unmatched = _file_risks(path, blocks, file_coverage.get("functions", {}))
        functions.extend(file_risks)
        unmatched.extend(file_unmatched)
    functions.sort(key=lambda item: (-item.crap_score, item.path, item.start_line, item.name))
    return {
        "schema_version": REPORT_SCHEMA_VERSION,
        "formula": FORMULA,
        "functions": [item.as_dict() for item in functions],
        "unmatched_coverage": sorted(unmatched),
    }


def _markdown_lines(payload: dict[str, Any]) -> list[str]:
    lines = [
        "# CRAP risk report",
        "",
        "CRAP = `complexity^2 * (1 - coverage)^3 + complexity`. "
        "Functions are sorted by descending score.",
        "",
        "| Path | Function | Complexity | Coverage | CRAP |",
        "| --- | --- | ---: | ---: | ---: |",
    ]
    lines.extend(
        f"| `{item['path']}` | `{item['name']}` | {item['complexity']} | "
        f"{item['coverage_percent']:.2f}% | {item['crap_score']:.2f} |"
        for item in payload["functions"]
    )
    return lines


def write_markdown_report(path: Path, payload: dict[str, Any]) -> None:
    _write_text(path, "\n".join(_markdown_lines(payload)) + "\n")


write_report = _write_json

__all__ = [
    "FORMULA",
    "REPORT_SCHEMA_VERSION",
    "FunctionRisk",
    "build_report",
    "write_markdown_report",
    "write_report",
]
