"""Exact copying, ordering, and verification behaviour of a portable payload.

A payload is assembled once on a frontend and then executed on a compute node
that cannot be inspected. The order files are listed in decides the stage
manifest's fingerprint, the copy must refuse anything that is not a plain file,
and the staged-file check must reject a file whose bytes changed even when its
size did not.
"""

from __future__ import annotations

import hashlib
from pathlib import Path

import pytest

from osm_polygon_description_tag.workflow import grid_operator
from osm_polygon_description_tag.workflow.grid_operator import (
    GridOperatorError,
    StagedFile,
)
from tests.helpers.messages import exactly


def _tree(parent: Path) -> Path:
    """Build a tree whose ordering separates a nested file from its sibling."""
    root = parent / "tree"
    (root / "a").mkdir(parents=True)
    (root / "a" / "b.txt").write_text("nested\n", encoding="utf-8")
    (root / "a.txt").write_text("sibling\n", encoding="utf-8")
    return root


def test_a_payload_lists_its_files_in_one_deterministic_order(tmp_path: Path) -> None:
    """The manifest's fingerprint is taken over this order, so the key is load-bearing.

    Sorting the paths themselves compares their parts and puts ``a/b.txt`` first;
    sorting their POSIX spellings puts ``a.txt`` first. The manifest uses the latter.
    """
    root = _tree(tmp_path)

    assert grid_operator._payload_files(root) == (root / "a.txt", root / "a" / "b.txt")


def test_project_source_files_are_listed_in_one_deterministic_order(tmp_path: Path) -> None:
    root = _tree(tmp_path)

    assert grid_operator._project_source_files(root) == (root / "a.txt", root / "a" / "b.txt")


def test_resume_files_are_listed_in_one_deterministic_order(tmp_path: Path) -> None:
    root = _tree(tmp_path)

    assert [item.relative_path for item in grid_operator._resume_files(root)] == [
        "a.txt",
        "a/b.txt",
    ]


def test_copying_into_an_existing_destination_is_not_an_error(tmp_path: Path) -> None:
    """A repeated stage copies into a directory that is already there."""
    source = tmp_path / "source"
    source.mkdir()
    (source / "file.txt").write_text("payload\n", encoding="utf-8")
    destination = tmp_path / "destination"
    destination.mkdir()

    grid_operator._copy_tree_no_symlinks(source, destination)

    assert (destination / "file.txt").read_text(encoding="utf-8") == "payload\n"


def test_a_source_that_is_not_a_directory_is_refused_under_its_own_label(
    tmp_path: Path,
) -> None:
    """The label names which side of the copy was wrong."""
    source = tmp_path / "source"
    source.write_text("not a directory", encoding="utf-8")

    with pytest.raises(
        GridOperatorError, match=exactly(f"source directory is not a regular directory: {source}")
    ):
        grid_operator._copy_tree_no_symlinks(source, tmp_path / "destination")


def test_a_retrieved_symlink_is_refused_and_names_the_path_it_found(tmp_path: Path) -> None:
    source = tmp_path / "link"
    source.symlink_to(tmp_path / "elsewhere")

    with pytest.raises(
        GridOperatorError,
        match=exactly(f"retrieved results contain a symlink: {source}"),
    ):
        grid_operator._copy_tree_entry(source, tmp_path / "destination")


def test_a_retrieved_non_file_is_refused_and_names_the_path_it_found(tmp_path: Path) -> None:
    import os

    source = tmp_path / "fifo"
    os.mkfifo(source)

    with pytest.raises(
        GridOperatorError,
        match=exactly(f"retrieved results contain a non-file: {source}"),
    ):
        grid_operator._copy_tree_entry(source, tmp_path / "destination")


def test_a_staged_file_whose_bytes_changed_without_its_size_is_refused(
    tmp_path: Path,
) -> None:
    """Equal sizes are common; only the digest can tell two payloads apart."""
    payload_root = tmp_path / "payload"
    payload_root.mkdir()
    path = payload_root / "job.sh"
    path.write_bytes(b"original")
    descriptor = StagedFile(
        relative_path="job.sh",
        size_bytes=len(b"original"),
        sha256=hashlib.sha256(b"tampered").hexdigest(),
    )

    with pytest.raises(GridOperatorError, match=exactly("staged file hash does not match: job.sh")):
        grid_operator._verify_staged_file(payload_root, descriptor)


def test_a_quarantine_destination_is_created_even_when_its_parent_exists(
    tmp_path: Path,
) -> None:
    """Quarantining a second artifact finds the quarantine directory already there."""
    from osm_polygon_description_tag.dataset.languages.checkpoint import shard_paths

    state = shard_paths(tmp_path, "region.parquet")
    state.parts.mkdir(parents=True)
    first = "parts/first.parquet"
    second = "parts/second.parquet"
    (state.root / first).write_bytes(b"first")
    (state.root / second).write_bytes(b"second")

    grid_operator._quarantine_artifact(state, first)
    grid_operator._quarantine_artifact(state, second)

    quarantine = state.root / grid_operator.QUARANTINE_DIRNAME
    assert (quarantine / first).read_bytes() == b"first"
    assert (quarantine / second).read_bytes() == b"second"


@pytest.mark.parametrize(
    ("field", "label"),
    [
        ("terminal_state", "terminal_state"),
        ("reconciled_at", "reconciled_at"),
        ("collected_at", "collected_at"),
    ],
)
def test_an_optional_intent_field_of_the_wrong_type_is_named_in_full(
    field: str, label: str
) -> None:
    """Six optional fields share this refusal, so it has to name which one failed."""
    with pytest.raises(
        GridOperatorError, match=exactly(f"intent field {label} must be a string or null")
    ):
        grid_operator._optional_text(7, label)
