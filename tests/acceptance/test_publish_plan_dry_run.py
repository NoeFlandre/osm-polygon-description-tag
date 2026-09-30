"""Acceptance: planning a publication shows the upload and sends nothing.

User story: as an operator I want to see exactly which files a publication would
upload, and its identity, without any network access or change to the data root.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from osm_polygon_description_tag.cli import run
from tests.helpers.publication import build_publication_dataset as _make_dataset

pytestmark = pytest.mark.acceptance


@pytest.fixture
def finished_build(tmp_path: Path) -> Path:
    data_root = tmp_path / "generated"
    _make_dataset(data_root)
    return data_root


def _snapshot(root: Path) -> dict[str, bytes]:
    return {
        path.relative_to(root).as_posix(): path.read_bytes()
        for path in sorted(root.rglob("*"))
        if path.is_file()
    }


def test_given_a_finished_build_when_planning_then_the_plan_lists_the_expected_files(
    finished_build: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    code = run(["publish-plan", "--data-root", str(finished_build)])

    plan = json.loads(capsys.readouterr().out)
    assert code == 0
    paths = {entry["relative_path"] for entry in plan["files"]}
    assert {"README.md", "stats.json", "data/a-latest.parquet"} <= paths
    assert len(plan["identity_sha256"]) == 64


def test_given_a_finished_build_when_planning_then_nothing_is_changed_or_sent(
    finished_build: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    before = _snapshot(finished_build.parent)

    run(["publish-plan", "--data-root", str(finished_build)])

    capsys.readouterr()
    assert _snapshot(finished_build.parent) == before


def test_given_the_same_build_when_planned_twice_then_the_identity_is_stable(
    finished_build: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    identities = []
    for _ in range(2):
        run(["publish-plan", "--data-root", str(finished_build)])
        identities.append(json.loads(capsys.readouterr().out)["identity_sha256"])

    assert identities[0] == identities[1]
