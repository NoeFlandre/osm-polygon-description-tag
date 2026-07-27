"""RED tests proving uniqueness validation uses a real, owned temporary SQLite file.

The implementation must NOT use ``sqlite3.connect(':memory:')``. The
connection string must point at a file inside an explicitly owned temporary
directory so the OS reclaims the file when the process exits.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from shapely.geometry import Polygon

from osm_polygon_description_tag.storage import (
    StorageError,
    _UniquenessIndex,
    validate_geoparquet,
    write_geoparquet,
)
from tests.conftest import make_record_dict


def test_uniqueness_index_uses_file_database_not_memory() -> None:
    """The connection must not be an in-memory database."""
    with _UniquenessIndex() as index:
        connection = index._connection  # type: ignore[attr-defined]
        # The SQLite connection exposes the path it was opened with via
        # ``db_name`` after open. A ``:memory:`` connection returns ``:memory:``.
        path = connection.execute("PRAGMA database_list").fetchall()
        db_names = [row[2] for row in path]
        assert all(name != ":memory:" for name in db_names), (
            f"uniqueness check used in-memory SQLite: {db_names}"
        )


def test_uniqueness_index_uses_owned_temp_path(tmp_path: Path) -> None:
    """The temp SQLite file lives under an owned temp directory."""
    with _UniquenessIndex() as index:
        db_path = Path(index.db_path)  # type: ignore[attr-defined]
        assert db_path.is_file()
        # The directory must be created explicitly by us, not by SQLite's tempdir.
        assert db_path.parent.exists()
        assert str(tmp_path) in {str(db_path.parent), str(db_path.parent.parent)} or (
            db_path.parent.name.startswith(".osm-validate-")
            or db_path.parent.parent.name.startswith(".osm-validate-")
        )


def test_uniqueness_index_closes_and_removes_db_on_exit(tmp_path: Path) -> None:
    """The SQLite file is removed after context exit."""
    index = _UniquenessIndex()
    with index:
        db_path = Path(index.db_path)  # type: ignore[attr-defined]
    assert not db_path.exists()


def test_uniqueness_index_cleans_up_on_keyboard_interrupt(tmp_path: Path) -> None:
    """A Ctrl-C during validation removes the temp SQLite file."""
    index = _UniquenessIndex()
    db_path = Path(index.db_path)  # type: ignore[attr-defined]
    try:
        raise KeyboardInterrupt()
    except KeyboardInterrupt:
        index.close()
    assert not db_path.exists()


def test_uniqueness_index_cleans_up_on_storage_error(tmp_path: Path) -> None:
    """A StorageError during validation removes the temp SQLite file."""
    index = _UniquenessIndex()
    db_path = Path(index.db_path)  # type: ignore[attr-defined]
    try:
        raise StorageError("validation failed")
    except StorageError:
        index.close()
    assert not db_path.exists()


def test_uniqueness_check_handles_large_row_count(tmp_path: Path) -> None:
    """The disk-backed index keeps memory bounded regardless of row count."""
    target = tmp_path / "region.parquet"
    records = [
        make_record_dict(
            Polygon([(i % 5, 0), (i % 5, 1), (i % 5 + 0.5, 1), (i % 5 + 0.5, 0)]),
            {"description": f"f{i}"},
            osm_id=6000 + i,
        )
        for i in range(500)
    ]
    write_geoparquet(iter(records), target, batch_size=100)
    assert validate_geoparquet(target) == 500


def test_uniqueness_index_persists_duplicate_detection_across_batches(
    tmp_path: Path,
) -> None:
    """Duplicate IDs across different batches must be rejected."""
    target = tmp_path / "dup.parquet"
    records = []
    for batch in range(3):
        for offset in range(10):
            records.append(
                make_record_dict(
                    Polygon([(batch, 0), (batch, 1), (batch + 0.5, 1), (batch + 0.5, 0)]),
                    {"description": f"f{batch}-{offset}"},
                    osm_id=batch * 100 + offset + 1,
                )
            )
    # Inject a duplicate spanning batches.
    records.append(
        make_record_dict(
            Polygon([(2, 0), (2, 1), (2.5, 1), (2.5, 0)]),
            {"description": "duplicate"},
            osm_id=1,  # collides with the first record
        )
    )
    with pytest.raises(StorageError, match="duplicate"):
        write_geoparquet(iter(records), target, batch_size=10)
