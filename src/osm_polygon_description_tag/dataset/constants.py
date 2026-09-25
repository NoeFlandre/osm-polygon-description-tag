"""Shared numeric defaults for dataset readers and writers."""

from __future__ import annotations

from typing import Final

DEFAULT_ARROW_BATCH_SIZE: Final = 4096
"""Rows per Arrow record batch when streaming DuckDB or Parquet reads."""

DEFAULT_WRITE_BATCH_SIZE: Final = 1024
"""Rows buffered per GeoParquet write batch."""
