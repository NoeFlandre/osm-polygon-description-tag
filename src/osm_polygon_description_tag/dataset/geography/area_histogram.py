"""Pure aggregation for the area distribution histogram.

The aggregator reads only the ``area_m2`` column from each finalized
Parquet file, bins every value into a logarithmic bucket, and returns a
deterministic mapping of bucket label to count. It performs no
rendering and no external I/O.

The bucketing scheme is fixed in :data:`AREA_BUCKETS` and :data:`AREA_BUCKET_LABELS`
to keep byte-for-byte PNG output reproducible across machines and runs.
"""

from __future__ import annotations

from collections.abc import Mapping
from pathlib import Path
from typing import Final

import pyarrow.parquet as pq

from osm_polygon_description_tag.dataset.geography.parquet_inputs import (
    sorted_parquets,
)

_AREA_HISTOGRAM_SCHEMA_VERSION: Final[int] = 1

# Logarithmic buckets covering m^2 from 0 to > 10^12 (the largest
# polygons in the dataset are country-sized, ~10^12 m^2). The bucket
# edges are inclusive-lower, exclusive-upper.
AREA_BUCKET_EDGES: Final[tuple[float, ...]] = (
    0.0,
    1.0,
    10.0,
    100.0,
    1_000.0,
    10_000.0,
    100_000.0,
    1_000_000.0,
    10_000_000.0,
    100_000_000.0,
    1_000_000_000.0,
    10_000_000_000.0,
    100_000_000_000.0,
)

AREA_BUCKET_LABELS: Final[tuple[str, ...]] = (
    "<1 m²",
    "1-10 m²",
    "10-100 m²",
    "100-1k m²",
    "1k-10k m²",
    "10k-100k m²",
    "100k-1M m²",
    "1M-10M m²",
    "10M-100M m²",
    "100M-1B m²",
    "1B-10B m²",
    "10B-100B m²",
    ">=100B m²",
)

AREA_BUCKET_COUNT: Final[int] = len(AREA_BUCKET_LABELS)

# Rendering version. Bump when bucket layout or visual constants change
# to invalidate every cached PNG at once.
AREA_HISTOGRAM_RENDER_VERSION: Final[int] = 1


def _bucket_index(area_m2: float) -> int:
    """Return the bucket index for a non-negative area in square metres.

    Areas below 0 (which should not exist in the published dataset) are
    clamped to the first bucket. Areas at or above the highest edge go
    to the last bucket.
    """
    if area_m2 < AREA_BUCKET_EDGES[1]:
        return 0
    upper = len(AREA_BUCKET_EDGES) - 1
    if area_m2 >= AREA_BUCKET_EDGES[upper]:
        return upper
    lo, hi = 1, upper - 1
    while lo <= hi:
        mid = (lo + hi) // 2
        if area_m2 < AREA_BUCKET_EDGES[mid]:
            hi = mid - 1
        else:
            lo = mid + 1
    return lo - 1


def aggregate_area_histogram(
    data_root: Path,
    *,
    batch_size: int = 8192,
) -> dict[str, int]:
    """Bucket every ``area_m2`` value into the fixed logarithmic buckets.

    Each finalized Parquet under ``data/`` contributes its rows exactly
    once. The histogram is keyed by :data:`AREA_BUCKET_LABELS` so callers
    never see a partial key set. The function never materialises the
    full dataset: only the ``area_m2`` column is read from each Parquet
    via :meth:`ParquetFile.iter_batches`.

    Parquet/manifest identity and GeoParquet payloads are validated via
    :func:`osm_polygon_description_tag.dataset.storage.validate_finalized_artifacts_strict`
    before streaming so mismatched, stale, or corrupt pairs cannot be
    observed as a partial histogram.

    An empty data directory yields all-zeros, preserving every label.
    """
    from osm_polygon_description_tag.dataset.storage import validate_finalized_artifacts_strict

    validate_finalized_artifacts_strict(data_root)
    counts: list[int] = [0] * AREA_BUCKET_COUNT
    for parquet_path in sorted_parquets(data_root / "data"):
        reader = pq.ParquetFile(parquet_path)
        for batch in reader.iter_batches(columns=("area_m2",), batch_size=batch_size):
            column = batch.column("area_m2").to_pylist()
            for value in column:
                if value is None:
                    continue
                counts[_bucket_index(float(value))] += 1
    return dict(zip(AREA_BUCKET_LABELS, counts, strict=False))


def area_histogram_input_sha256(
    file_output_sha256s: Mapping[str, str],
) -> str:
    """Stable identity for the area-histogram cache.

    The histogram is recomputed only when finalized Parquet output
    identities change. ``file_output_sha256s`` maps each Parquet path
    (relative to ``data/``) to the SHA-256 of its bytes.
    """
    import hashlib
    import json

    payload = {
        "cache_schema_version": _AREA_HISTOGRAM_SCHEMA_VERSION,
        "render_version": AREA_HISTOGRAM_RENDER_VERSION,
        "files": [
            {"parquet": name, "output_sha256": sha}
            for name, sha in sorted(file_output_sha256s.items())
        ],
    }
    encoded = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


__all__ = [
    "AREA_BUCKET_COUNT",
    "AREA_BUCKET_EDGES",
    "AREA_BUCKET_LABELS",
    "AREA_HISTOGRAM_RENDER_VERSION",
    "aggregate_area_histogram",
    "area_histogram_input_sha256",
]
