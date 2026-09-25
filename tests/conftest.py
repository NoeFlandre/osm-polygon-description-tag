"""Shared pytest fixtures producing realistic schema-conformant records."""

import os
import shlex
import shutil
import socket
import subprocess
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest
from shapely import to_wkb

from osm_polygon_description_tag.extraction import ExportRecord
from osm_polygon_description_tag.transform import transform_record


class NetworkAccessInTestError(RuntimeError):
    """Raised when a test tries to open a connection off this machine."""


@pytest.fixture(autouse=True)
def _no_outbound_network(monkeypatch: pytest.MonkeyPatch) -> None:
    """Refuse outbound connections, loudly, from every test.

    A unit test must not depend on the network, but the stronger reason is
    mutation testing. Several mutants bypass an injected HTTP session, and the
    code then falls back to building a real one and calling the Hugging Face
    dataset-viewer API. That call does not fail fast: the worker hangs and dies
    on a signal, and the gate records a segfault rather than a killed mutant.
    Refusing the connection turns that into an ordinary assertion failure, so
    the mutant is killed for the reason it should be.

    Loopback stays open: local servers and socketpairs are not the network.
    """
    real_connect = socket.socket.connect

    def guarded_connect(self: socket.socket, address: object) -> object:
        host = address[0] if isinstance(address, tuple) and address else None
        if host in {"127.0.0.1", "::1", "localhost", None}:
            return real_connect(self, address)  # type: ignore[arg-type]
        raise NetworkAccessInTestError(
            f"a test tried to connect to {host!r}; tests must not use the network"
        )

    monkeypatch.setattr(socket.socket, "connect", guarded_connect)


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
    monkeypatch.setenv("OSM_POLYGON_DESCRIPTION_TAG_TRACKIO", "0")
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


FAKE_OSMIUM_VERSION = "osmium version 1.19.1"


@pytest.fixture
def fake_osmium(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Put a stand-in ``osmium`` first on ``PATH`` for tests that pass preflight.

    Preflight only resolves the executable and reads ``--version``. Unit tests
    that go through it must not depend on osmium-tool being installed, so this
    script answers the version probe the way the real binary does.
    """
    bin_dir = tmp_path / "fake-osmium-bin"
    bin_dir.mkdir()
    script = bin_dir / "osmium"
    script.write_text(
        f"#!/bin/sh\necho '{FAKE_OSMIUM_VERSION}'\necho 'libosmium version 2.20.0'\n",
        encoding="utf-8",
    )
    script.chmod(0o755)
    monkeypatch.setenv("PATH", f"{bin_dir}{os.pathsep}{os.environ.get('PATH', '')}")
    return script


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
