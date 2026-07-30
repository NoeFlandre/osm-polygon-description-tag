"""Shared pytest fixtures producing realistic schema-conformant records."""

import os
import shlex
import shutil
import subprocess
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest
from shapely import to_wkb
from shapely.geometry import MultiPolygon, Polygon

from osm_polygon_description_tag.extraction import ExportRecord
from osm_polygon_description_tag.transform import transform_record


def _is_test_owned_executable(executable: str) -> bool:
    try:
        resolved = Path(executable).resolve(strict=False)
    except OSError:
        return False
    return any(part.startswith(("pytest-", "pytest-of-")) for part in resolved.parts)


def _reject_live_hf_command(command: object) -> None:
    if isinstance(command, bytes):
        command = os.fsdecode(command)
    if isinstance(command, str):
        try:
            lexer = shlex.shlex(command, posix=True, punctuation_chars=";&|")
            lexer.whitespace_split = True
            tokens = list(lexer)
        except ValueError:
            tokens = command.split()
    elif isinstance(command, list | tuple):
        tokens = [os.fspath(part) for part in command]
    else:
        return
    if not tokens:
        return
    for index, executable in enumerate(tokens[:-1]):
        if Path(executable).name != "hf":
            continue
        if tokens[index + 1] not in {"auth", "upload", "upload-large-folder"}:
            continue
        if _is_test_owned_executable(executable):
            continue
        raise RuntimeError(
            "refusing to launch real Hugging Face CLI from tests; "
            "patch the defining runner or use a pytest-owned fake executable"
        )


@pytest.fixture(autouse=True)
def _fail_closed_hf_subprocess_guard(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> Iterator[None]:
    real_popen = subprocess.Popen
    real_which = shutil.which
    fake_hf = tmp_path / "hf"
    fake_hf.write_text("#!/bin/sh\necho 'fake-user'\n", encoding="utf-8")
    fake_hf.chmod(0o755)

    def guarded_popen(command: object, *args: object, **kwargs: Any) -> subprocess.Popen[Any]:
        _reject_live_hf_command(command)
        return real_popen(command, *args, **kwargs)

    def hermetic_which(name: str) -> str | None:
        if name == "hf":
            return str(fake_hf)
        return real_which(name)

    class _HermeticHfApi:
        def whoami(self) -> object:
            return {"name": "fake-user"}

        def repo_info(self, *_args: object, **_kwargs: object) -> object:
            return type("_RepoInfo", (), {"sha": "test-revision"})()

        def auth_check(self, *_args: object, **_kwargs: object) -> None:
            return None

    monkeypatch.setattr(subprocess, "Popen", guarded_popen)
    monkeypatch.setattr("shutil.which", hermetic_which)
    monkeypatch.setattr(
        "osm_polygon_description_tag.workflow.preflight._huggingface_hub.HfApi",
        _HermeticHfApi,
        raising=False,
    )
    yield


def _ewkb_hex(geom: object) -> str:
    return to_wkb(geom, include_srid=True, flavor="extended", byte_order=1).hex()  # type: ignore[arg-type]


def make_export_record(
    geom: object,
    tags: dict[str, str],
    *,
    osm_type: str = "way",
    osm_id: int = 1,
) -> ExportRecord:
    return ExportRecord(
        geometry_ewkb_hex=_ewkb_hex(geom),
        osm_type=osm_type,
        osm_id=osm_id,
        version=1,
        changeset=10,
        timestamp="2026-01-01T00:00:00Z",
        tags=tags,
    )


def make_record_dict(
    geom: object,
    tags: dict[str, str],
    *,
    osm_type: str = "way",
    osm_id: int = 1,
    source_pbf: str = "region.osm.pbf",
) -> dict[str, object]:
    return transform_record(
        make_export_record(geom, tags, osm_type=osm_type, osm_id=osm_id), source_pbf
    )


@pytest.fixture
def way_record_dict() -> dict[str, object]:
    return make_record_dict(
        Polygon([(0, 0), (0, 1), (1, 1), (1, 0)]),
        {"description": "A building", "building": "yes"},
        osm_type="way",
        osm_id=100,
    )


@pytest.fixture
def relation_record_dict() -> dict[str, object]:
    geom = MultiPolygon(
        [
            Polygon([(10, 10), (10, 11), (11, 11), (11, 10)]),
            Polygon([(20, 20), (20, 21), (21, 21), (21, 20)]),
        ]
    )
    return make_record_dict(
        geom,
        {"description:en": "Two parts", "description:pt-BR": "Duas partes"},
        osm_type="relation",
        osm_id=200,
    )


@pytest.fixture
def valid_records(
    way_record_dict: dict[str, object], relation_record_dict: dict[str, object]
) -> list[dict[str, object]]:
    return [way_record_dict, relation_record_dict]


@pytest.fixture
def record_stream(valid_records: list[dict[str, object]]) -> Iterator[dict[str, object]]:
    yield from valid_records
