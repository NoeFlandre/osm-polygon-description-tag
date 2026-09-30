"""Synthetic CLI replies and shell stubs for multi-shard Grid driver tests."""

from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any

import pytest

import osm_polygon_description_tag.workflow.grid_driver as driver
from osm_polygon_description_tag.workflow.grid_driver import DriverOptions, Remote

CLI = str(Path(sys.executable).parent / "osm-polygon-description-tag")


def remote() -> Remote:
    return Remote(
        "nancy",
        "/home/op/bundles",
        "/home/op/models/glotlid-v3/model_v3.bin",
        "/home/op/models/sat-3l-sm/model.safetensors",
    )


def options(**overrides: Any) -> DriverOptions:
    fields: dict[str, Any] = {
        "run_dir": Path("/run"),
        "source_root": Path("/source"),
        "retrieval_dir": Path("/retrieve"),
        "remote_bundle_root": "/home/op/bundles",
        "remote_glotlid_model_path": "/home/op/models/glotlid-v3/model_v3.bin",
        "remote_sat_model_path": "/home/op/models/sat-3l-sm/model.safetensors",
        "remote_operator_dir": "/home/op/project",
        "remote_cli": "/home/op/.venv/bin/osm-polygon-description-tag",
    }
    return DriverOptions(**{**fields, **overrides})


def plan() -> dict[str, str]:
    return {
        "remote_project_dir": "/home/op/bundles/x/project",
        "remote_source_dir": "/home/op/bundles/x/source",
        "remote_run_dir": "/home/op/bundles/x/run",
    }


def apply_payload(outcome: str, *, job_id: int | None = 6923144) -> dict[str, Any]:
    """Return the nested payload printed by ``language grid submit --apply``."""
    return {
        "applied": True,
        "plan": {"shard": "region.parquet", "may_apply": True},
        "result": {
            "outcome": outcome,
            "job_id": job_id,
            "detail": "job submitted",
            "attempt": 1,
        },
    }


def write_checkpoint(run_dir: Path, slug: str, payload: object) -> Path:
    shard_dir = run_dir / "shards" / slug
    shard_dir.mkdir(parents=True)
    checkpoint = shard_dir / "checkpoint.json"
    if isinstance(payload, str):
        checkpoint.write_text(payload, encoding="utf-8")
    else:
        checkpoint.write_text(json.dumps(payload), encoding="utf-8")
    return checkpoint


def intent(**overrides: object) -> str:
    payload = {
        "outcome": "submitted",
        "result_acknowledged": False,
        "job_id": 1234,
        "shard": "angola-latest.parquet",
    }
    payload.update(overrides)
    return json.dumps(payload)


def stub_ssh(monkeypatch: pytest.MonkeyPatch, stdout: str) -> None:
    class Completed:
        def __init__(self) -> None:
            self.stdout = stdout
            self.returncode = 0

    monkeypatch.setattr(
        "osm_polygon_description_tag.workflow.grid_driver.subprocess.run",
        lambda *args, **kwargs: Completed(),
    )


class Shell:
    """Record driver commands and return ordered subprocess-like replies."""

    def __init__(self, *replies: tuple[int, str, str]) -> None:
        self.replies = list(replies)
        self.calls: list[tuple[object, dict[str, object]]] = []

    def __call__(self, argv: object, **kwargs: object) -> Any:
        self.calls.append((argv, kwargs))
        code, stdout, stderr = self.replies.pop(0) if self.replies else (0, "{}", "")
        return type("Completed", (), {"returncode": code, "stdout": stdout, "stderr": stderr})()

    @property
    def argvs(self) -> list[object]:
        return [argv for argv, _ in self.calls]


def shell(monkeypatch: pytest.MonkeyPatch, *replies: tuple[int, str, str]) -> Shell:
    instance = Shell(*replies)
    monkeypatch.setattr(driver.subprocess, "run", instance)
    return instance
