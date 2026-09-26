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

import json
import uuid
from collections.abc import Sequence
from concurrent.futures import ProcessPoolExecutor
from contextlib import closing
from dataclasses import replace
from pathlib import Path
from typing import cast

import pyarrow as pa
import pyarrow.parquet as pq

from osm_polygon_description_tag.dataset.constants import DEFAULT_ARROW_BATCH_SIZE
from osm_polygon_description_tag.dataset.manifest import (
    Manifest,
    _manifest_path_for,
    output_identity_for,
    read_manifest,
    write_manifest,
)
from osm_polygon_description_tag.dataset.migration import (
    _promote_migrated_parquet,
    _require_migration_directories,
)
from osm_polygon_description_tag.dataset.schema import SCHEMA, geo_metadata
from osm_polygon_description_tag.dataset.storage import (
    _DICTIONARY_COLUMNS,
    GEOPARQUET_COMPRESSION,
    StorageError,
    validate_geoparquet,
)
from osm_polygon_description_tag.dataset.text import (
    has_successful_description_text,
    trimmed_nonempty_text,
)

REJECTION_REASON = "no_nonempty_description"

_BATCH_SIZE = DEFAULT_ARROW_BATCH_SIZE


class TextMigrationError(RuntimeError):
    """Raised when a legacy artifact's text cannot be repaired safely."""


def _canonical_entry(item: object) -> dict[str, str] | None:
    """Return one trimmed localized entry, or ``None`` when it carries no text."""
    if not isinstance(item, dict):
        return None
    key = item.get("key")
    trimmed = trimmed_nonempty_text(item.get("value"))
    if not isinstance(key, str) or trimmed is None:
        return None
    return {"key": key, "value": trimmed}


def _is_entry_sequence(value: object) -> bool:
    """Return whether a value is a sequence of localized entries."""
    return isinstance(value, Sequence) and not isinstance(value, str | bytes)


def _canonical_localized(value: object) -> list[dict[str, str]]:
    """Return localized entries with trimmed values, dropping blank ones."""
    if not _is_entry_sequence(value):
        return []
    entries = cast(Sequence[object], value)  # pragma: no mutate - static narrowing
    candidates = map(_canonical_entry, entries)
    return [entry for entry in candidates if entry is not None]


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


def _text_columns(table: pa.Table) -> tuple[list[object], list[object]]:
    """Return the canonical text columns, leaving every other column untouched.

    Only ``description`` and ``localized_descriptions`` are in scope. The other
    columns are carried through as Arrow read them, so a repair can never
    reorder ``tags``, collapse a duplicated key, or drop a null-valued entry.
    """
    descriptions: list[object] = []
    localized: list[object] = []
    described = table.column("description").to_pylist()
    localized_entries = table.column("localized_descriptions").to_pylist()
    # Arrow refuses to build a table whose columns differ in length, so the
    # strict zip cannot fire here. It is kept as a guard for a caller that
    # assembles the two lists itself, and excluded from mutation because no
    # input can tell it apart from a plain zip.
    for description, entries in zip(described, localized_entries, strict=True):  # pragma: no mutate
        descriptions.append(trimmed_nonempty_text(description))
        localized.append(_canonical_localized(entries))
    return descriptions, localized


def _retained_mask(descriptions: list[object], localized: list[object]) -> list[bool]:
    return [
        has_successful_description_text(description, entries)
        for description, entries in zip(descriptions, localized, strict=True)
    ]


def _canonical_table(table: pa.Table) -> tuple[pa.Table, int]:
    """Return the repaired table and the number of rows it drops."""
    descriptions, localized = _text_columns(table)
    repaired = table.set_column(
        table.schema.get_field_index("description"),
        SCHEMA.field("description"),
        pa.array(descriptions, SCHEMA.field("description").type),
    ).set_column(
        table.schema.get_field_index("localized_descriptions"),
        SCHEMA.field("localized_descriptions"),
        pa.array(localized, SCHEMA.field("localized_descriptions").type),
    )
    mask = _retained_mask(descriptions, localized)
    if all(mask):
        return repaired, 0
    return repaired.filter(pa.array(mask)), mask.count(False)


def _geo_metadata_for(table: pa.Table, inherited: dict[bytes, bytes]) -> pa.Schema:
    """Rebuild the ``geo`` block so it describes the rows actually retained."""
    geometry_types = [value for value in table.column("geometry_type").to_pylist() if value]
    # Only ever consumed for truthiness by geo_metadata, where [] and None
    # are indistinguishable.
    bbox: list[float] = []  # pragma: no mutate
    if table.num_rows:
        bbox = [
            min(table.column("bbox_min_x").to_pylist()),
            min(table.column("bbox_min_y").to_pylist()),
            max(table.column("bbox_max_x").to_pylist()),
            max(table.column("bbox_max_y").to_pylist()),
        ]
    metadata = dict(inherited)
    metadata[b"geo"] = json.dumps(geo_metadata(geometry_types, bbox)).encode()
    return SCHEMA.with_metadata(metadata)


def _rewrite_parquet_text(
    reader: pq.ParquetFile,
    temporary: Path,
    metadata: pa.Schema,
) -> int:
    """Write the canonical artifact and return the number of dropped rows."""
    repaired, dropped = _canonical_table(reader.read())
    schema = metadata if not dropped else _geo_metadata_for(repaired, metadata.metadata or {})
    with pq.ParquetWriter(
        temporary,
        schema,
        compression=GEOPARQUET_COMPRESSION,
        use_dictionary=_DICTIONARY_COLUMNS,
    ) as writer:
        writer.write_table(repaired.cast(schema))
    validate_geoparquet(temporary)
    return dropped


def _requires_text_migration(path: Path) -> bool:
    """Return whether any stored description value is not already canonical."""
    with closing(pq.ParquetFile(path)) as reader:
        for batch in reader.iter_batches(
            batch_size=_BATCH_SIZE,
            columns=["description", "localized_descriptions"],
        ):
            for row in batch.to_pylist():
                canonical = _canonical_row(row)
                if canonical is None or _row_changed(row, canonical):
                    return True
    return False


def _migrate_parquet_text(path: Path) -> int | None:
    """Repair one artifact, returning dropped rows, or ``None`` when clean."""
    if not _requires_text_migration(path):
        return None

    temporary = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
    try:
        with closing(pq.ParquetFile(path)) as reader:
            metadata = SCHEMA.with_metadata(reader.schema_arrow.metadata or {})
            dropped = _rewrite_parquet_text(reader, temporary, metadata)
        _promote_migrated_parquet(temporary, path)
        return dropped
    except (OSError, pa.ArrowException, StorageError) as error:
        raise TextMigrationError(f"cannot migrate {path}: {error}") from error
    finally:
        if temporary.exists():
            temporary.unlink()


def _migrate_one_artifact(parquet: Path, manifest_path: Path) -> int:
    dropped = _migrate_parquet_text(parquet)
    manifest = read_manifest(manifest_path)
    if dropped is None:
        return _heal_output_identity(manifest, parquet, manifest_path)
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


def _heal_output_identity(manifest: Manifest, parquet: Path, manifest_path: Path) -> int:
    """Refresh a manifest left stale by an interrupted earlier run.

    A crash between promoting a Parquet and writing its manifest leaves the
    repaired artifact on disk under the previous identity. The text is already
    canonical by then, so no rewrite is needed and the counts are already
    correct; only the recorded identity has to catch up.
    """
    identity = output_identity_for(parquet)
    if manifest.output == identity:
        return 0
    write_manifest(replace(manifest, output=identity), manifest_path)
    return 1


def _artifact_pair(parquet: Path, data_root: Path) -> tuple[Path, Path]:
    return parquet, _manifest_path_for(parquet.name, data_root)


def _migration_artifact_pairs(data_root: Path) -> list[tuple[Path, Path]]:
    """Validate the migration roots and return their Parquet/manifest pairs."""
    data_dir = data_root / "data"
    manifests_dir = data_root / "manifests"
    _require_migration_directories(data_dir, manifests_dir, data_root, error=TextMigrationError)
    # pragma: no mutate start - children share a parent, so name and path order agree
    artifacts = sorted(data_dir.glob("*.parquet"), key=lambda path: path.name)
    # pragma: no mutate end
    return [_artifact_pair(parquet, data_root) for parquet in artifacts]


def migrate_dataset_text(data_root: Path, *, max_workers: int | None = None) -> int:
    """Repair untrimmed description text and return the files changed.

    Each Parquet is promoted before its manifest is updated, so interruption
    leaves a safe per-file resume point. An artifact whose text is already
    canonical is left byte-identical, which makes a second run a no-op.

    Artifacts are independent, so they may be repaired concurrently. The
    result does not depend on the worker count: each worker owns one artifact
    and its manifest, and the returned total is order-independent.
    """
    pairs = _migration_artifact_pairs(data_root)
    if max_workers is not None and max_workers > 1:
        return _migrate_concurrently(pairs, max_workers)
    return sum(_migrate_one_artifact(parquet, manifest) for parquet, manifest in pairs)


def _migrate_concurrently(pairs: list[tuple[Path, Path]], max_workers: int) -> int:
    with ProcessPoolExecutor(max_workers=max_workers) as pool:
        futures = [
            pool.submit(_migrate_one_artifact, parquet, manifest) for parquet, manifest in pairs
        ]
        return sum(future.result() for future in futures)


__all__ = ["TextMigrationError", "migrate_dataset_text"]
