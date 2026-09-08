"""Portable state paths cannot escape their declared relative namespace."""

from pathlib import Path

import pytest

from osm_polygon_description_tag.dataset.languages.paths import relative_posix_path


@pytest.mark.parametrize(
    ("value", "diagnostic"),
    [
        ("../escape", "must not contain traversal"),
        ("nested/../escape", "must not contain traversal"),
        ("/absolute", "must be relative and portable"),
        ("nested\\file", "must be relative and portable"),
        (".", "must not contain traversal"),
        ("", "must be a non-empty relative path"),
        (None, "must be a non-empty relative path"),
        (7, "must be a non-empty relative path"),
        (b"nested/file.parquet", "must be a non-empty relative path"),
    ],
)
def test_unsafe_state_paths_are_rejected(value: object, diagnostic: str) -> None:
    with pytest.raises(RuntimeError) as caught:
        relative_posix_path(value, error=RuntimeError, label="receipt shard")
    assert str(caught.value) == f"receipt shard {diagnostic}"


@pytest.mark.parametrize("value", ["nested/file.parquet", Path("nested/file.parquet")])
def test_portable_nested_paths_keep_their_spelling(value: str | Path) -> None:
    assert relative_posix_path(value, error=ValueError, label="shard") == "nested/file.parquet"
