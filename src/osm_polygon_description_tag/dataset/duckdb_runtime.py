"""Configure the bounded-memory DuckDB connections used for dataset queries."""

from __future__ import annotations

from pathlib import Path

import duckdb


def open_data_connection(data_root: Path) -> duckdb.DuckDBPyConnection:
    """Open an in-memory query connection whose spill files stay under the data root."""
    work_root = data_root / ".work" / "duckdb"
    work_root.mkdir(parents=True, exist_ok=True)
    connection = duckdb.connect(":memory:")
    connection.execute("SET temp_directory = ?", [str(work_root)])
    return connection
