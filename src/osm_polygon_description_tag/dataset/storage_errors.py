"""The error raised by GeoParquet writing, validation and artifact checks."""

from __future__ import annotations


class StorageError(ValueError):
    """Raised for integrity or infrastructure failures during storage."""


__all__ = ["StorageError"]
