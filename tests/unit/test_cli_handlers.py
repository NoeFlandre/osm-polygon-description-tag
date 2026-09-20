"""Contracts for the dataset CLI handlers.

These handlers are thin, which is exactly why they are worth pinning: each one
forwards operator-supplied arguments into an operation that writes or publishes
data, and prints the JSON another tool consumes. A dropped argument here is a
silent change of behaviour with nothing to show for it.
"""

from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

import osm_polygon_description_tag.cli as cli


@pytest.fixture
def paths(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> SimpleNamespace:
    """Resolve paths from the handler's own arguments, and prove it did.

    A stub that ignores what it is handed cannot tell whether the handler
    passed its arguments or something else entirely, which is exactly the
    mutation it is meant to catch: `_resolve_paths(None)` would still be
    handed back a valid Paths object and every assertion would pass.
    """
    resolved = SimpleNamespace(source_root=tmp_path / "sources", data_root=tmp_path / "data-root")

    def _resolve(args: object) -> SimpleNamespace:
        assert isinstance(args, SimpleNamespace), f"handler passed {args!r} instead of its args"
        return resolved

    monkeypatch.setattr(cli, "_resolve_paths", _resolve)
    return resolved


def test_migrate_text_forwards_the_worker_count_and_reports_the_file_count(
    paths: SimpleNamespace, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    seen: dict[str, Any] = {}

    def _migrate(data_root: Path, *, max_workers: int) -> int:
        seen["data_root"] = data_root
        seen["max_workers"] = max_workers
        return 3

    monkeypatch.setattr(cli, "migrate_dataset_text", _migrate)

    assert cli.handle_migrate_text(SimpleNamespace(max_workers=4)) == 0
    assert seen == {"data_root": paths.data_root, "max_workers": 4}
    assert json.loads(capsys.readouterr().out) == {
        "data_root": str(paths.data_root),
        "migrated_files": 3,
    }


def test_release_stats_forwards_the_repo_confirmation_and_apply_gate(
    paths: SimpleNamespace, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """``apply`` is the gate between planning and publishing; it must not be lost."""
    seen: dict[str, Any] = {}

    class Report:
        def to_payload(self) -> dict[str, object]:
            return {"revision": "abc123"}

    def _release(data_root: Path, template: str, *, confirm_repo: str, apply: bool) -> Report:
        seen.update(data_root=data_root, template=template, confirm_repo=confirm_repo, apply=apply)
        return Report()

    monkeypatch.setattr(cli, "release_metadata", _release)
    monkeypatch.setattr(cli, "dataset_card_template", lambda: "TEMPLATE")

    args = SimpleNamespace(confirm_repo="owner/dataset", apply=True)

    assert cli.handle_release_stats(args) == 0
    assert seen == {
        "data_root": paths.data_root,
        "template": "TEMPLATE",
        "confirm_repo": "owner/dataset",
        "apply": True,
    }
    assert json.loads(capsys.readouterr().out) == {"revision": "abc123"}


def test_release_stats_defaults_to_planning_when_apply_is_not_requested(
    paths: SimpleNamespace, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    seen: dict[str, Any] = {}

    class Report:
        def to_payload(self) -> dict[str, object]:
            return {}

    monkeypatch.setattr(
        cli,
        "release_metadata",
        lambda data_root, template, *, confirm_repo, apply: (
            seen.update(apply=apply),
            Report(),
        )[1],
    )
    monkeypatch.setattr(cli, "dataset_card_template", lambda: "TEMPLATE")

    assert cli.handle_release_stats(SimpleNamespace(confirm_repo="owner/dataset", apply=False)) == 0
    assert seen == {"apply": False}
    capsys.readouterr()
