"""Atomic migration of legacy untrimmed description text to canonical form.

Artifacts built before the successful-text contract stored description values
exactly as they appeared in OSM, including leading and trailing whitespace.
The final-artifact contract requires the canonical trimmed form, so those
artifacts cannot be published until the stored text is repaired.

The repair is the same normalization the current build path applies
(``trim_values=True`` in :mod:`~osm_polygon_description_tag.dataset.transform`),
so a migrated artifact matches what a rebuild from the raw PBF would produce
for this defect. A value that is only whitespace carries no text, so it is
dropped; a row left without any description text is excluded and recorded
under the same ``no_nonempty_description`` reason the build path uses.

The raw PBF source root is never read or modified.
"""

from __future__ import annotations

import os
import uuid
from collections.abc import Callable, Sequence
from concurrent.futures import ProcessPoolExecutor
from dataclasses import replace
from pathlib import Path
from typing import cast

import pyarrow as pa
import pyarrow.parquet as pq

from osm_polygon_description_tag.dataset.manifest import (
    _manifest_path_for,
    output_identity_for,
    read_manifest,
    write_manifest,
)
from osm_polygon_description_tag.dataset.schema import SCHEMA
from osm_polygon_description_tag.dataset.storage import (
    StorageError,
    _arrow_record,
    validate_geoparquet,
)
from osm_polygon_description_tag.dataset.text import (
    has_successful_description_text,
    trimmed_nonempty_text,
)

REJECTION_REASON = "no_nonempty_description"

_BATCH_SIZE = 4096


class TextMigrationError(RuntimeError):
    """Raised when a legacy artifact's text cannot be repaired safely."""


def _canonical_localized(value: object) -> list[dict[str, str]]:
    """Return localized entries with trimmed values, dropping blank ones."""
    if not isinstance(value, Sequence) or isinstance(value, str | bytes):
        return []
    entries: list[dict[str, str]] = []
    for item in cast(Sequence[object], value):
        if not isinstance(item, dict):
            continue
        key = item.get("key")
        trimmed = trimmed_nonempty_text(item.get("value"))
        if isinstance(key, str) and trimmed is not None:
            entries.append({"key": key, "value": trimmed})
    return entries


def _canonical_row(row: dict[str, object]) -> dict[str, object] | None:
    """Return the canonical row, or ``None`` when it carries no text."""
    repaired = dict(row)
    repaired["description"] = trimmed_nonempty_text(row.get("description"))
    repaired["localized_descriptions"] = _canonical_localized(row.get("localized_descriptions"))
    if not has_successful_description_text(
        repaired["description"],
        repaired["localized_descriptions"],
    ):
        return None
    return repaired


def _row_changed(before: dict[str, object], after: dict[str, object]) -> bool:
    stored = before.get("localized_descriptions")
    previous = list(stored) if isinstance(stored, Sequence) else []
    return (
        before.get("description") != after["description"]
        or previous != after["localized_descriptions"]
    )


def _rewrite_parquet_text(
    reader: pq.ParquetFile,
    temporary: Path,
    metadata: pa.Schema,
) -> int:
    """Write the canonical artifact and return the number of dropped rows."""
    dropped = 0
    with pq.ParquetWriter(temporary, metadata, compression="zstd") as writer:
        for batch in reader.iter_batches(batch_size=_BATCH_SIZE):
            rows: list[dict[str, object]] = []
            for row in batch.to_pylist():
                canonical = _canonical_row(row)
                if canonical is None:
                    dropped += 1
                    continue
                rows.append(_arrow_record(canonical))
            writer.write_table(pa.Table.from_pylist(rows, schema=metadata))
    validate_geoparquet(temporary)
    return dropped


def _requires_text_migration(path: Path) -> bool:
    """Return whether any stored description value is not already canonical."""
    reader = pq.ParquetFile(path)
    for batch in reader.iter_batches(
        batch_size=_BATCH_SIZE,
        columns=["description", "localized_descriptions"],
    ):
        for row in batch.to_pylist():
            canonical = _canonical_row(row)
            if canonical is None or _row_changed(row, canonical):
                return True
    return False


def _promote_migrated_parquet(temporary: Path, target: Path) -> None:
    with open(temporary, "rb") as handle:
        os.fsync(handle.fileno())
    os.replace(temporary, target)


def _migrate_parquet_text(
    path: Path,
    rewrite: Callable[..., int] | None = None,
) -> int | None:
    """Repair one artifact, returning dropped rows, or ``None`` when clean."""
    if not _requires_text_migration(path):
        return None

    reader = pq.ParquetFile(path)
    metadata = SCHEMA.with_metadata(reader.schema_arrow.metadata or {})
    temporary = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
    writer = rewrite if rewrite is not None else _rewrite_parquet_text
    try:
        dropped = writer(reader, temporary, metadata)
        _promote_migrated_parquet(temporary, path)
        return dropped if dropped is not None else 0
    except (OSError, pa.ArrowException, StorageError) as error:
        raise TextMigrationError(f"cannot migrate {path}: {error}") from error
    finally:
        if temporary.exists():
            temporary.unlink()


def _migrate_one_artifact(parquet: Path, manifest_path: Path) -> int:
    dropped = _migrate_parquet_text(parquet)
    if dropped is None:
        return 0
    manifest = read_manifest(manifest_path)
    counts = manifest.counts
    rejections = dict(counts.rejections)
    if dropped:
        rejections[REJECTION_REASON] = rejections.get(REJECTION_REASON, 0) + dropped
    write_manifest(
        replace(
            manifest,
            output=output_identity_for(parquet),
            counts=replace(
                counts,
                included_rows=counts.included_rows - dropped,
                rejections=rejections,
            ),
        ),
        manifest_path,
    )
    return 1


def _require_migration_directories(data_dir: Path, manifests_dir: Path, data_root: Path) -> None:
    if not data_dir.is_dir() or not manifests_dir.is_dir():
        raise TextMigrationError(f"missing data/ or manifests/ under {data_root}")


def _artifact_pair(parquet: Path, data_root: Path) -> tuple[Path, Path]:
    return parquet, _manifest_path_for(parquet.name, data_root)


def migrate_dataset_text(data_root: Path, *, max_workers: int | None = None) -> int:
    """Repair untrimmed description text and return the files changed.

    Each Parquet is promoted before its manifest is updated, so interruption
    leaves a safe per-file resume point. An artifact whose text is already
    canonical is left byte-identical, which makes a second run a no-op.

    Artifacts are independent, so they may be repaired concurrently. The
    result does not depend on the worker count: each worker owns one artifact
    and its manifest, and the returned total is order-independent.
    """
    data_dir = data_root / "data"
    manifests_dir = data_root / "manifests"
    _require_migration_directories(data_dir, manifests_dir, data_root)
    pairs = [
        _artifact_pair(parquet, data_root)
        for parquet in sorted(data_dir.glob("*.parquet"), key=lambda path: path.name)
    ]
    if max_workers is not None and max_workers > 1 and len(pairs) > 1:
        return _migrate_concurrently(pairs, max_workers)
    return sum(_migrate_one_artifact(parquet, manifest) for parquet, manifest in pairs)


def _migrate_concurrently(pairs: list[tuple[Path, Path]], max_workers: int) -> int:
    with ProcessPoolExecutor(max_workers=max_workers) as pool:
        futures = [
            pool.submit(_migrate_one_artifact, parquet, manifest) for parquet, manifest in pairs
        ]
        return sum(future.result() for future in futures)


__all__ = ["TextMigrationError", "migrate_dataset_text"]
