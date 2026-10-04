"""Contracts for the reproducible description-tag container workflow."""

from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path

import pytest

PROJECT_ROOT = Path(__file__).resolve().parents[2]


def _read(name: str) -> str:
    return (PROJECT_ROOT / name).read_text(encoding="utf-8")


def test_runtime_image_is_locked_safe_and_has_required_osm_tools() -> None:
    dockerfile = _read("Dockerfile")

    assert "ARG UV_IMAGE=ghcr.io/astral-sh/uv:0.11.16" in dockerfile
    assert "FROM ${UV_IMAGE} AS uv" in dockerfile
    assert "FROM python:3.12-slim-bookworm AS base" in dockerfile
    assert "COPY --from=uv /uv /uvx /usr/local/bin/" in dockerfile
    assert "apt-get install -y --no-install-recommends libpcre2-8-0 osmium-tool" in dockerfile
    assert "uv sync --frozen --no-dev --no-editable" in dockerfile
    assert "USER app" in dockerfile
    assert 'ENTRYPOINT ["osm-polygon-description-tag"]' in dockerfile
    assert 'CMD ["--help"]' in dockerfile
    assert 'VOLUME ["/data"]' in dockerfile
    assert "HOME=/tmp" in dockerfile
    assert "HF_TOKEN" not in dockerfile


def test_dockerignore_excludes_data_secrets_and_local_deliverables() -> None:
    dockerignore = _read(".dockerignore")

    for pattern in (
        ".git/",
        ".venv/",
        "slides/",
        "data/",
        "manifests/",
        ".cache/",
        ".work/",
        "logs/",
        "*.osm.pbf",
        "*.parquet",
        ".env",
        ".huggingface/",
        ".DS_Store",
    ):
        assert pattern in dockerignore


def test_justfile_exposes_one_safe_resumable_container_command() -> None:
    justfile = _read("justfile")

    for recipe in ("docker-build:", "docker-help:", "docker-test:", "docker-check:"):
        assert recipe in justfile
    assert "docker-run data_root: docker-build" in justfile
    assert '--user "$(id -u):$(id -g)"' in justfile
    assert "dst=/source,readonly" in justfile
    assert "run-and-publish" in justfile
    assert "--source-root /source" in justfile
    assert "--data-root /data" in justfile
    assert 'repo_id := "NoeFlandre/osm-polygon-description-tag"' in justfile
    assert "--confirm-repo {{repo_id}}" in justfile


def test_docs_describe_the_container_boundary_and_resume_contract() -> None:
    development = _read("docs/development.md")
    architecture = _read("docs/architecture.md")

    for text in (
        "Docker reproducibility",
        "uv.lock",
        "osmium-tool",
        "read-only",
        "/source",
        "Ctrl-C",
        "HF_TOKEN",
    ):
        assert text in development
    for text in ("Dockerfile", "non-root", "/data", "run-and-publish"):
        assert text in architecture


def test_quality_ci_builds_and_smoke_tests_the_runtime_image() -> None:
    workflow = _read(".github/workflows/quality.yml")

    assert "docker build" in workflow
    assert "--target runtime" in workflow
    assert "docker run --rm" in workflow
    assert "--help" in workflow
    assert "osmium" in workflow


def test_quality_ci_keeps_the_fixable_vulnerability_gate_and_updates_bookworm_pcre2() -> None:
    dockerfile = _read("Dockerfile")
    workflow = _read(".github/workflows/quality.yml")

    assert "apt-get install -y --no-install-recommends libpcre2-8-0 osmium-tool" in dockerfile
    assert "severity: HIGH,CRITICAL" in workflow
    assert 'exit-code: "1"' in workflow
    assert "ignore-unfixed: true" in workflow
    assert "CVE-2026-103111" not in _read(".trivyignore")


@pytest.mark.integration
def test_opt_in_docker_smoke() -> None:
    """Build and run the image only when explicitly requested."""

    if os.environ.get("RUN_DOCKER_SMOKE") != "1":
        pytest.skip("set RUN_DOCKER_SMOKE=1 to run the Docker smoke test")
    docker = shutil.which("docker")
    if docker is None:
        pytest.skip("Docker CLI is not installed")
    daemon = subprocess.run(  # noqa: S603 - executable is resolved from PATH.
        [docker, "info"], capture_output=True, text=True, check=False
    )
    if daemon.returncode != 0:
        pytest.skip(f"Docker daemon is unavailable: {daemon.stderr.strip()}")

    image = "osm-polygon-description-tag:test"
    build = subprocess.run(  # noqa: S603 - executable is resolved from PATH.
        [docker, "build", "--target", "runtime", "--tag", image, str(PROJECT_ROOT)],
        capture_output=True,
        text=True,
        check=False,
    )
    assert build.returncode == 0, build.stdout + build.stderr
    help_run = subprocess.run(  # noqa: S603 - executable is resolved from PATH.
        [docker, "run", "--rm", image, "--help"],
        capture_output=True,
        text=True,
        check=False,
    )
    assert help_run.returncode == 0, help_run.stdout + help_run.stderr
    assert "run-and-publish" in help_run.stdout


def _last_stage(dockerfile: str) -> list[list[str]]:
    stages: list[list[list[str]]] = []
    for line in dockerfile.replace("\\\n", " ").splitlines():
        words = line.split()
        if not words or words[0].startswith("#"):
            continue
        if words[0].upper() == "FROM":
            stages.append([])
        if stages:
            stages[-1].append(words)
    return stages[-1]


def test_the_runtime_image_presets_its_roots_and_carries_oci_labels() -> None:
    stage = _last_stage(_read("Dockerfile"))
    env = [word for words in stage if words[0].upper() == "ENV" for word in words[1:]]
    labels = " ".join(word for words in stage if words[0].upper() == "LABEL" for word in words[1:])

    assert "OSM_POLYGON_SOURCE_ROOT=/source" in env
    assert "OSM_POLYGON_DATA_ROOT=/data" in env
    for key in ("source", "licenses", "title"):
        assert f"org.opencontainers.image.{key}=" in labels


def test_the_compose_pipeline_mounts_raw_input_read_only_and_runs_as_the_caller() -> None:
    import yaml

    service = yaml.safe_load(_read("compose.yaml"))["services"]["pipeline"]
    mounts = {volume["target"]: volume for volume in service["volumes"]}

    assert service["build"]["target"] == "runtime"
    assert mounts["/source"]["read_only"] is True
    assert "read_only" not in mounts["/data"]
    assert "UID" in service["user"]
    assert "HF_TOKEN" not in service["environment"]
    assert service["env_file"] == [{"path": ".env", "required": False}]
