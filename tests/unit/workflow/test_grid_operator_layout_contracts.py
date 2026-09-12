"""Exact paths, labels, and boundary values of the Grid'5000 operator's helpers.

These are the values that decide what a compute node actually runs: which
directory owns a job, which files travel in a portable payload, which argument
a refusal names, and where a budget's accepted range ends. A filesystem
assertion cannot pin a path's spelling on a case-insensitive volume, so the
paths here are compared as paths, by value.
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from osm_polygon_description_tag.dataset.languages.checkpoint import shard_paths, shards_root
from osm_polygon_description_tag.workflow import grid_operator
from osm_polygon_description_tag.workflow.grid_operator import (
    MAX_PROCESSING_SECONDS,
    MAX_WALLTIME_SECONDS,
    GridOperatorError,
    job_paths,
    jobs_root,
)
from tests.helpers.messages import exactly


def test_a_run_directory_owns_its_shards_and_jobs_under_fixed_lowercase_names(
    tmp_path: Path,
) -> None:
    """The compute nodes are case-sensitive, so these two names are exact."""
    assert shards_root(tmp_path) == tmp_path / "shards"
    assert jobs_root(tmp_path) == tmp_path / "jobs"


def test_a_jobs_directory_is_named_by_the_first_half_of_its_bundle_identifier(
    tmp_path: Path,
) -> None:
    """A different prefix length would silently split one job across two directories."""
    bundle = SimpleNamespace(bundle_id="b" * 64)

    paths = job_paths(tmp_path, bundle)  # type: ignore[arg-type]

    assert paths.root == tmp_path / "jobs" / ("b" * 32)


def test_the_payload_carries_the_lowercase_src_directory(tmp_path: Path) -> None:
    """``src`` is the import root on the compute node and is spelled exactly."""
    assert grid_operator._project_source_root(tmp_path) == tmp_path / "src"


def test_the_project_files_are_the_two_required_files_the_readme_and_the_source(
    tmp_path: Path,
) -> None:
    """Each name is what the compute node looks for; ``README.md`` is case-sensitive."""
    (tmp_path / "pyproject.toml").write_text("[project]\n", encoding="utf-8")
    (tmp_path / "uv.lock").write_text("version = 1\n", encoding="utf-8")
    (tmp_path / "README.md").write_text("# readme\n", encoding="utf-8")
    source = tmp_path / "src"
    source.mkdir()
    (source / "module.py").write_text("VALUE = 1\n", encoding="utf-8")

    assert grid_operator._project_files(tmp_path) == (
        tmp_path / "pyproject.toml",
        tmp_path / "uv.lock",
        tmp_path / "README.md",
        source / "module.py",
    )


def test_compiled_python_artifacts_are_treated_as_cache_whatever_their_suffix(
    tmp_path: Path,
) -> None:
    """A stale ``.pyo`` shipped to a compute node would shadow the real module."""
    assert grid_operator._is_project_cache_file(tmp_path / "m.pyc", Path("m.pyc"))
    assert grid_operator._is_project_cache_file(tmp_path / "m.pyo", Path("m.pyo"))
    assert not grid_operator._is_project_cache_file(tmp_path / "m.py", Path("m.py"))


@pytest.mark.parametrize(
    ("base", "name", "expected"),
    [("/", "run", "/run"), ("/home/user", "run", "/home/user/run")],
)
def test_a_remote_child_of_the_bundle_root_is_joined_once(
    base: str, name: str, expected: str
) -> None:
    assert grid_operator._remote_child(base, name) == expected


@pytest.mark.parametrize("value", ["/a/../b", "/.."])
def test_a_remote_path_with_a_traversal_component_is_refused_under_its_label(
    value: str,
) -> None:
    """OAR resolves the stored command remotely, so no ``..`` may survive into it."""
    with pytest.raises(
        GridOperatorError,
        match=exactly("remote run directory must not contain traversal components"),
    ):
        grid_operator._validate_remote_path(value, "remote run directory")


@pytest.mark.parametrize(
    ("value", "message"),
    [
        (0, f"walltime must be between 1 and {MAX_WALLTIME_SECONDS} seconds"),
        (
            MAX_WALLTIME_SECONDS + 1,
            f"walltime must be between 1 and {MAX_WALLTIME_SECONDS} seconds",
        ),
    ],
)
def test_a_walltime_outside_the_accepted_range_is_refused_exactly(value: int, message: str) -> None:
    with pytest.raises(GridOperatorError, match=exactly(message)):
        grid_operator._validate_walltime_seconds(value)


def test_the_smallest_and_largest_walltimes_are_both_accepted() -> None:
    """Both ends of the documented range must be usable, not just the middle."""
    assert grid_operator._validate_walltime_seconds(1) == 1
    assert grid_operator._validate_walltime_seconds(MAX_WALLTIME_SECONDS) == MAX_WALLTIME_SECONDS


def test_the_smallest_processing_budget_is_accepted() -> None:
    assert grid_operator._validate_processing_seconds(1) == 1
    assert (
        grid_operator._validate_processing_seconds(MAX_PROCESSING_SECONDS) == MAX_PROCESSING_SECONDS
    )


def test_a_one_second_submission_walltime_is_positive_and_accepted() -> None:
    """One second is positive; refusing it would refuse a legal request."""
    assert grid_operator._validate_submission_walltime(1) is None


@pytest.mark.parametrize("value", [0, -1])
def test_a_non_positive_submission_walltime_is_refused_exactly(value: int) -> None:
    with pytest.raises(
        GridOperatorError,
        match=exactly("walltime must be a positive number of seconds for this workload"),
    ):
        grid_operator._validate_submission_walltime(value)


def test_a_job_name_resolver_may_return_the_smallest_positive_job_id(tmp_path: Path) -> None:
    """Job 1 is a real job id; refusing it would strand a reconcilable job."""
    intent_path = tmp_path / "intent.json"
    intent = grid_operator.SubmissionIntent(
        bundle_id="b" * 64,
        shard="region.parquet",
        job_name="osm-language-region",
        walltime_seconds=1800,
        cores=1,
        recorded_at="2026-09-15T22:00:00+00:00",
        outcome="ambiguous",
    )
    paths = SimpleNamespace(intent=intent_path)

    updated, job_id = grid_operator._resolved_by_name(paths, intent, lambda _name: 1)  # type: ignore[arg-type,misc]

    assert job_id == 1
    assert updated.job_id == 1
    assert json.loads(intent_path.read_text(encoding="utf-8"))["job_id"] == 1


def test_an_empty_resume_state_fingerprints_the_shard_and_an_empty_file_list(
    tmp_path: Path,
) -> None:
    """Both payload keys are part of the fingerprint another site has to reproduce."""
    shard = "region.parquet"
    expected = hashlib.sha256(
        grid_operator.canonical_json_bytes({"shard": shard, "files": []})
    ).hexdigest()

    assert grid_operator._resume_state_fingerprint(tmp_path, shard) == expected


def test_resume_artifacts_without_a_checkpoint_are_refused_with_their_exact_reason(
    tmp_path: Path,
) -> None:
    """Fingerprinting half a shard's state would stage a job that resumes from nothing."""
    state = shard_paths(tmp_path, "region.parquet")
    state.parts.mkdir(parents=True)

    with pytest.raises(
        GridOperatorError, match=exactly("resume artifacts exist without a checkpoint")
    ):
        grid_operator._resume_state_fingerprint(tmp_path, "region.parquet")


@pytest.mark.parametrize("shape", ["symlink", "file"])
def test_a_resume_state_root_that_is_not_a_directory_is_refused_exactly(
    tmp_path: Path, shape: str
) -> None:
    state = shard_paths(tmp_path, "region.parquet")
    state.root.parent.mkdir(parents=True)
    if shape == "symlink":
        state.root.symlink_to(tmp_path / "elsewhere")
        message = "resume state directory must not be a symlink"
    else:
        state.root.write_text("not a directory", encoding="utf-8")
        message = "resume state path must be a directory"

    with pytest.raises(GridOperatorError, match=exactly(message)):
        grid_operator._resume_state_fingerprint(tmp_path, "region.parquet")


def test_quarantined_artifacts_are_skipped_without_stopping_the_rest(tmp_path: Path) -> None:
    """A ``break`` here would silently drop every file after the quarantine."""
    root = tmp_path / "state"
    quarantine = root / grid_operator.QUARANTINE_DIRNAME
    quarantine.mkdir(parents=True)
    (quarantine / "old.parquet").write_bytes(b"quarantined")
    (root / "zz-last.json").write_text("{}", encoding="utf-8")

    descriptors = grid_operator._resume_files(root)

    assert [descriptor.relative_path for descriptor in descriptors] == ["zz-last.json"]
