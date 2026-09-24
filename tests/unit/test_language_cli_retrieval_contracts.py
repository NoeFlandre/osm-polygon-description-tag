"""Exact remote and local paths used when a finished shard is retrieved.

Collection copies state off the site and imports it under both locks. A wrong
remote child, a wrong snapshot filename, or a missing staging directory would
either retrieve nothing or import the wrong shard's state, so each path is
asserted by value.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from osm_polygon_description_tag.workflow.grid_commands import (
    _prepare_retrieval_directory,
    _remote_child,
    _require_retrieved_run_dir,
    _retrieval_snapshot_source,
    _retrieval_snapshot_target,
)
from osm_polygon_description_tag.workflow.grid_operator import GridOperatorError


@pytest.mark.parametrize(
    ("base", "name", "expected"),
    [
        ("/home/user/bundle", "run", "/home/user/bundle/run"),
        ("/home/user/bundle/", "run", "/home/user/bundle//run"),
        ("/", "run", "/run"),
        ("/home/user/bundle", "source", "/home/user/bundle/source"),
    ],
)
def test_a_remote_child_is_joined_with_exactly_one_separator(
    base: str, name: str, expected: str
) -> None:
    assert _remote_child(base, name) == expected


def test_a_bundle_root_that_is_only_slashes_still_yields_an_absolute_child() -> None:
    """`rstrip('/')` empties `///`, and the fallback must keep the path absolute."""
    base = "///".rstrip("/") or "/"

    assert _remote_child(base, "run") == "/run"


def test_the_snapshot_is_copied_between_the_two_lowercase_filenames(tmp_path: Path) -> None:
    """The compute nodes are case-sensitive, so the name must be exact."""
    local = tmp_path / "run"
    local.mkdir()
    (local / "snapshot.json").write_text("{}", encoding="utf-8")
    retrieved = tmp_path / "retrieved"
    retrieved.mkdir()

    source = _retrieval_snapshot_source(local)
    target = _retrieval_snapshot_target(retrieved)

    assert [entry.name for entry in local.iterdir()] == ["snapshot.json"]
    assert source == local / "snapshot.json"
    assert target == retrieved / "snapshot.json"


def test_a_missing_local_run_snapshot_is_refused_by_its_exact_path(tmp_path: Path) -> None:
    local = tmp_path / "run"
    local.mkdir()

    with pytest.raises(GridOperatorError) as caught:
        _retrieval_snapshot_source(local)

    assert str(caught.value) == f"local run snapshot is missing: {local / 'snapshot.json'}"


def test_a_missing_retrieved_run_dir_is_refused_with_its_exact_reason() -> None:
    with pytest.raises(GridOperatorError) as caught:
        _require_retrieved_run_dir(None)

    assert str(caught.value) == (
        "remote collection requires --retrieved-run-dir for its local staging area"
    )


def test_a_supplied_retrieved_run_dir_is_returned_unchanged(tmp_path: Path) -> None:
    assert _require_retrieved_run_dir(tmp_path) == tmp_path


def test_the_retrieval_directory_is_created_including_missing_parents(tmp_path: Path) -> None:
    nested = tmp_path / "a" / "b" / "retrieved"

    _prepare_retrieval_directory(nested)

    assert nested.is_dir()


def test_preparing_an_existing_retrieval_directory_is_not_an_error(tmp_path: Path) -> None:
    nested = tmp_path / "retrieved"
    nested.mkdir()

    _prepare_retrieval_directory(nested)

    assert nested.is_dir()
