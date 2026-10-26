from __future__ import annotations

import re
import tomllib
from collections import Counter
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[2]
COMPLEXITY_RULES = frozenset({"C901", "PLR0912", "PLR0913", "PLR0915"})
BASELINE_CONFIG_SUPPRESSIONS = frozenset(
    {
        ("src/osm_polygon_description_tag/dataset/languages/worker.py", "PLR0913"),
        ("src/osm_polygon_description_tag/workflow/grid_workflow.py", "PLR0913"),
        ("src/osm_polygon_description_tag/language_cli.py", "PLR0913"),
        ("src/osm_polygon_description_tag/publication/state.py", "PLR0913"),
        ("src/osm_polygon_description_tag/workflow/build.py", "PLR0913"),
        ("src/osm_polygon_description_tag/workflow/finalization.py", "PLR0913"),
        ("src/osm_polygon_description_tag/workflow/grid_operator/bundle.py", "PLR0913"),
        ("src/osm_polygon_description_tag/workflow/grid_operator/script.py", "PLR0913"),
        ("src/osm_polygon_description_tag/workflow/grid_operator/stage.py", "PLR0913"),
        ("src/osm_polygon_description_tag/workflow/grid_operator/submission.py", "PLR0913"),
        ("src/osm_polygon_description_tag/workflow/grid_policy.py", "PLR0913"),
        ("src/osm_polygon_description_tag/workflow/orchestrator.py", "PLR0913"),
        ("src/osm_polygon_description_tag/workflow/source_runner.py", "PLR0913"),
    }
)
BASELINE_INLINE_SUPPRESSIONS = {"C901": 5, "PLR0912": 0, "PLR0913": 0, "PLR0915": 4}


def test_file_level_complexity_suppressions_are_tracked_and_only_decrease() -> None:
    project_text = (PROJECT_ROOT / "pyproject.toml").read_text(encoding="utf-8")
    project = tomllib.loads(project_text)
    ignores = project["tool"]["ruff"]["lint"]["per-file-ignores"]
    actual = {
        (path, rule)
        for path, rules in ignores.items()
        for rule in COMPLEXITY_RULES.intersection(rules)
    }

    assert actual <= BASELINE_CONFIG_SUPPRESSIONS
    lines = project_text.splitlines()
    for path, rule in actual:
        config_line = next(
            line for line in lines if line.lstrip().startswith(f'"{path}"') and f'"{rule}"' in line
        )
        assert "TODO(#141)" in config_line


def test_inline_complexity_noqa_suppressions_are_tracked_and_only_decrease() -> None:
    noqa_pattern = re.compile(
        r"# noqa:\s*((?:C901|PLR0912|PLR0913|PLR0915)"
        r"(?:\s*,\s*(?:C901|PLR0912|PLR0913|PLR0915))*)"
    )
    actual: Counter[str] = Counter()
    for source_dir in ("src", "tests", "scripts"):
        for path in (PROJECT_ROOT / source_dir).rglob("*.py"):
            for line_number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
                match = noqa_pattern.search(line)
                if match is None:
                    continue
                rules = [rule.strip() for rule in match.group(1).split(",")]
                assert "TODO(#62)" in line, f"{path}:{line_number} lacks a tracking issue"
                actual.update(rules)

    assert all(actual[rule] <= baseline for rule, baseline in BASELINE_INLINE_SUPPRESSIONS.items())
