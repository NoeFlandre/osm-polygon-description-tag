"""collect_h3_counts equals a slow per-identity reference (#82 golden check).

The dataset mixes cross-shard duplicates, versions, rows whose text fails
the text contract (so text-aware ranking differs from plain ranking) and
geometries near the antimeridian.
"""

from __future__ import annotations

import random
from collections import Counter, defaultdict
from pathlib import Path

import pytest
from shapely import from_wkb
from shapely.geometry import Polygon

from osm_polygon_description_tag.dataset import storage_validation
from osm_polygon_description_tag.dataset.canonical_rows import (
    canonical_rows_with_text_flag_sql,
    select_canonical_row,
)
from osm_polygon_description_tag.dataset.geography.h3_policy import (
    DEFAULT_H3_RESOLUTION,
    assign_h3_cell,
)
from osm_polygon_description_tag.dataset.geography.parquet_inputs import (
    H3AggregationError,
    collect_h3_counts,
)
from osm_polygon_description_tag.dataset.text import description_row_has_successful_text
from tests.conftest import make_record_dict
from tests.helpers.dataset import write_finalized_dataset


def _square(lon: float, lat: float, size: float) -> Polygon:
    return Polygon([(lon, lat), (lon + size, lat), (lon + size, lat + size), (lon, lat + size)])


def _shards(seed: int) -> dict[str, list[dict[str, object]]]:
    rng = random.Random(seed)  # noqa: S311 - deterministic test data, not security
    shards: dict[str, list[dict[str, object]]] = {"a": [], "b": [], "c": []}
    for osm_id in range(1, 301):
        for name in rng.sample(sorted(shards), rng.randint(1, 3)):
            lon = rng.choice([rng.uniform(-179.9, -179.0), rng.uniform(178.5, 179.4)])
            if rng.random() < 0.7:
                lon = rng.uniform(-170, 170)
            row = make_record_dict(
                _square(lon, rng.uniform(-80, 80), 0.3),
                {"description": f"place {osm_id}"},
                osm_id=osm_id,
                source_pbf=f"{name}.osm.pbf",
            )
            row["version"] = rng.randint(1, 4)
            if rng.random() < 0.3:
                # No description and no localized value: fails the text contract.
                row["description"] = None
                row["localized_descriptions"] = []
            shards[name].append(row)
    return shards


def _reference_counts(shards: dict[str, list[dict[str, object]]]) -> dict[str, int]:
    groups: dict[tuple[object, object], list[dict[str, object]]] = defaultdict(list)
    for rows in shards.values():
        for row in rows:
            groups[(row["osm_type"], row["osm_id"])].append(row)
    counts: Counter[str] = Counter()
    for rows in groups.values():
        if not any(description_row_has_successful_text(row) for row in rows):
            continue
        row = select_canonical_row(rows, require_successful_text=True)
        centroid = from_wkb(row["geometry"]).centroid
        counts[assign_h3_cell(centroid.y, centroid.x, resolution=DEFAULT_H3_RESOLUTION)] += 1
    return dict(sorted(counts.items()))


def _write_legacy_dataset(
    data_root: Path, sources: Path, shards: dict[str, list[dict[str, object]]], monkeypatch
) -> None:
    """Write rows that fail the text contract, as legacy artifacts can contain."""
    real = storage_validation._validate_description_values

    def lenient(description: object, localized: object, **_kwargs: object) -> None:
        real(description, localized, require_successful_text=False)

    with monkeypatch.context() as patched:
        patched.setattr(storage_validation, "_validate_description_values", lenient)
        write_finalized_dataset(data_root, sources, shards)


def test_collect_h3_counts_matches_the_per_identity_reference(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    shards = _shards(82)
    data_root = tmp_path / "data-root"
    _write_legacy_dataset(data_root, tmp_path / "sources", shards, monkeypatch)

    counts = collect_h3_counts(data_root)

    assert counts == _reference_counts(shards)
    assert 150 < sum(counts.values()) < 300  # text filtering removed some identities


def test_invalid_geometry_in_a_text_rejected_top_row_still_fails(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A row outside the text population is validated, never silently skipped."""
    good = make_record_dict(_square(10, 10, 0.3), {"description": "ok"}, osm_id=7)
    bow_tie = Polygon([(0, 0), (1, 1), (1, 0), (0, 1)])  # self-intersecting
    top = make_record_dict(_square(20, 20, 0.3), {"description": "x"}, osm_id=7)
    top["geometry"] = bow_tie.wkb
    top["version"] = 9  # outranks the valid-text row
    top["description"] = None
    top["localized_descriptions"] = []
    data_root = tmp_path / "data-root"
    top["source_pbf"] = "b.osm.pbf"
    with monkeypatch.context() as patched:
        # Legacy artifacts predate the write-time geometry check.
        patched.setattr(storage_validation, "_validate_geometry", lambda _geometry, _type: None)
        _write_legacy_dataset(data_root, tmp_path / "sources", {"a": [good], "b": [top]}, patched)

    with pytest.raises(H3AggregationError, match="invalid or empty geometry"):
        collect_h3_counts(data_root)


def test_the_flagged_query_rejects_unknown_or_missing_columns() -> None:
    with pytest.raises(ValueError, match="at least one"):
        canonical_rows_with_text_flag_sql("t", [])
    with pytest.raises(ValueError, match="unsupported"):
        canonical_rows_with_text_flag_sql("t", ["nope"])
