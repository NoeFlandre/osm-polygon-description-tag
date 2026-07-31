"""Aggregation, rendering, and cache tests for the area distribution histogram.

The histogram is the dataset-card replacement for the per-stat ``Spatial
summary`` table. These tests pin the bucketing logic, byte-stable
rendering, atomic writes, mtime preservation, and cache-identity reuse.
"""

from __future__ import annotations

import hashlib
import json
import time
from pathlib import Path

import matplotlib
import pyarrow as pa
import pyarrow.parquet as pq
import pytest

matplotlib.use("Agg")

from osm_polygon_description_tag.dataset.geography import (
    AREA_BUCKET_COUNT,
    AREA_BUCKET_EDGES,
    AREA_BUCKET_LABELS,
    AREA_HISTOGRAM_RENDER_VERSION,
    AreaHistogramResult,
    aggregate_area_histogram,
    area_histogram_input_sha256,
    render_area_histogram,
)
from osm_polygon_description_tag.dataset.geography.area_histogram import (
    _bucket_index,
)
from osm_polygon_description_tag.dataset.geography.area_rendering import (
    _atomic_save_png,
    atomic_save_png_for_testing,
)
from osm_polygon_description_tag.dataset.manifest import (
    Manifest,
    RunCounts,
    current_area_policy_sha256,
    current_output_algorithm_revision,
    output_identity_for,
    source_identity_for,
    write_manifest,
)
from osm_polygon_description_tag.dataset.reporting import (
    _area_histogram_input_sha256,
    generate_dataset_docs,
)
from tests.unit.dataset.test_reporting import _frozen_clock, _populate_dataset


def _write_parquet_with_areas(directory: Path, name: str, areas: list[float]) -> Path:
    """Write a single-column Parquet that the histogram can stream from."""
    table = pa.table({"area_m2": pa.array(areas, type=pa.float64())})
    target = directory / f"{name}.parquet"
    pq.write_table(table, target)
    return target


def _make_finalized_area_histogram_data_root(tmp_path: Path, files: dict[str, list[float]]) -> Path:
    """Write Parquet + matching manifest pairs so validation passes."""
    data_root = tmp_path / "generated"
    source_root = tmp_path / "raw"
    (data_root / "data").mkdir(parents=True)
    (data_root / "manifests").mkdir(parents=True)
    source_root.mkdir(exist_ok=True)
    for stem, areas in files.items():
        source = source_root / f"{stem}.osm.pbf"
        source.write_bytes(stem.encode("utf-8"))
        output = data_root / "data" / f"{stem}.parquet"
        _write_parquet_with_areas(data_root / "data", stem, areas)
        write_manifest(
            Manifest(
                manifest_schema_version=2,
                schema_version=2,
                geoparquet_version="1.1.0",
                transform_algorithm_version=2,
                area_policy_sha256=current_area_policy_sha256(),
                output_algorithm_revision=current_output_algorithm_revision(),
                source=source_identity_for(source),
                output=output_identity_for(output),
                osmium_version=None,
                dependency_versions={"pyarrow": "20.0.0"},
                code_revision=None,
                started_at="2026-01-01T00:00:00+00:00",
                completed_at="2026-01-01T00:00:01+00:00",
                counts=RunCounts(
                    emitted_features=len(areas),
                    included_rows=len(areas),
                    rejections={},
                ),
            ),
            data_root / "manifests" / f"{stem}.manifest.json",
        )
    return data_root


# --- bucketing -----------------------------------------------------------


def test_bucket_index_handles_bucket_lower_edges() -> None:
    """Lower bucket edges (inclusive) round down to the right index."""
    for index, edge in enumerate(AREA_BUCKET_EDGES[:-1]):
        assert _bucket_index(edge) == index


def test_bucket_index_handles_one_under_upper_edge() -> None:
    """Values just below an upper edge land in the lower of the two bins.

    The epsilon avoids IEEE-754 rounding (subtracting ``1e-6`` from
    ``100_000_000_000.0`` yields ``100_000_000_000.0`` exactly).
    """
    for index in range(len(AREA_BUCKET_EDGES) - 1):
        upper = AREA_BUCKET_EDGES[index + 1]
        epsilon = upper * 1e-6
        assert _bucket_index(upper - epsilon) == index


def test_bucket_index_handles_above_highest_edge() -> None:
    """Areas at or above the top edge end up in the final bucket."""
    top = AREA_BUCKET_EDGES[-1]
    assert _bucket_index(top) == AREA_BUCKET_COUNT - 1
    assert _bucket_index(top * 1_000_000_000.0) == AREA_BUCKET_COUNT - 1


def test_bucket_index_handles_negative_values() -> None:
    """Negative areas clamp to the smallest bucket (defensive: dataset rejects these)."""
    assert _bucket_index(-1.0) == 0


def test_bucket_index_handles_zero() -> None:
    assert _bucket_index(0.0) == 0


# --- aggregation ---------------------------------------------------------


def test_aggregate_area_histogram_returns_zeroed_labels_for_empty_dataset(
    tmp_path: Path,
) -> None:
    """An empty (but finalized) data root returns the all-zero label set."""
    data_root = _make_finalized_area_histogram_data_root(tmp_path, {})
    counts = aggregate_area_histogram(data_root)
    assert dict(counts) == {label: 0 for label in AREA_BUCKET_LABELS}


def test_aggregate_area_histogram_buckets_finite_areas(tmp_path: Path) -> None:
    """Every area in the dataset must land in exactly one bucket, summed exactly once."""
    # One area per bucket: the lower edge of every populated bucket plus a
    # value above the top edge for the open-ended ``>100B m²`` bucket.
    areas = [edge for edge in AREA_BUCKET_EDGES[:-1]] + [AREA_BUCKET_EDGES[-1] * 10.0]
    data_root = _make_finalized_area_histogram_data_root(tmp_path, {"regions": areas})
    counts = aggregate_area_histogram(data_root)
    assert all(counts[label] == 1 for label in AREA_BUCKET_LABELS)
    assert sum(counts.values()) == len(areas)


def test_aggregate_area_histogram_handles_large_areas(tmp_path: Path) -> None:
    data_root = _make_finalized_area_histogram_data_root(tmp_path, {"big": [3.5e12, 1.2e10, 0.0]})
    counts = aggregate_area_histogram(data_root)
    assert counts["<1 m²"] == 1
    assert counts[">100B m²"] == 1
    assert counts["10B-100B m²"] == 1


def test_aggregate_area_histogram_sums_across_multiple_parquets(tmp_path: Path) -> None:
    data_root = _make_finalized_area_histogram_data_root(
        tmp_path, {"a": [10.0, 100.0], "b": [10.0, 1_000.0]}
    )
    counts = aggregate_area_histogram(data_root)
    assert counts["10-100 m²"] == 2
    assert counts["100-1k m²"] == 1
    assert counts["1k-10k m²"] == 1


def test_aggregate_area_histogram_is_byte_stable(tmp_path: Path) -> None:
    data_root = _make_finalized_area_histogram_data_root(
        tmp_path, {"x": [0.0, 1.0, 10.0, 100.0, 1_000.0]}
    )
    first = aggregate_area_histogram(data_root)
    second = aggregate_area_histogram(data_root)
    assert first == second
    # Also stable as JSON, not just dict equality.
    assert json.dumps(first, sort_keys=True) == json.dumps(second, sort_keys=True)


def test_aggregate_area_histogram_skips_null_areas(tmp_path: Path) -> None:
    """A null ``area_m2`` row is skipped; the rest lands in the right bucket."""
    data_root = tmp_path / "generated"
    source_root = tmp_path / "raw"
    (data_root / "data").mkdir(parents=True)
    (data_root / "manifests").mkdir(parents=True)
    source_root.mkdir()
    source = source_root / "with_nulls.osm.pbf"
    source.write_bytes(b"with_nulls")
    table = pa.table({"area_m2": pa.array([10.0, None, 100.0], type=pa.float64())})
    output = data_root / "data" / "with_nulls.parquet"
    pq.write_table(table, output)
    write_manifest(
        Manifest(
            manifest_schema_version=2,
            schema_version=2,
            geoparquet_version="1.1.0",
            transform_algorithm_version=2,
            area_policy_sha256=current_area_policy_sha256(),
            output_algorithm_revision=current_output_algorithm_revision(),
            source=source_identity_for(source),
            output=output_identity_for(output),
            osmium_version=None,
            dependency_versions={"pyarrow": "20.0.0"},
            code_revision=None,
            started_at="2026-01-01T00:00:00+00:00",
            completed_at="2026-01-01T00:00:01+00:00",
            counts=RunCounts(emitted_features=3, included_rows=2, rejections={}),
        ),
        data_root / "manifests" / "with_nulls.manifest.json",
    )
    counts = aggregate_area_histogram(data_root)
    assert counts["10-100 m²"] == 1
    assert counts["100-1k m²"] == 1
    assert sum(counts.values()) == 2


def test_aggregate_area_histogram_rejects_orphan_parquet(tmp_path: Path) -> None:
    """An orphan Parquet (no matching manifest) must be rejected, matching the H3 contract."""
    from osm_polygon_description_tag.dataset.storage import StorageError

    (tmp_path / "data").mkdir(parents=True)
    _write_parquet_with_areas(tmp_path / "data", "lonely", [10.0])
    # No manifest/ directory or matching manifest: validation must raise.
    with pytest.raises(StorageError):
        aggregate_area_histogram(tmp_path)


# --- cache identity ------------------------------------------------------


def test_area_histogram_input_sha256_changes_with_parquet_hash() -> None:
    mapping = {"a.parquet": hashlib.sha256(b"one").hexdigest()}
    first = area_histogram_input_sha256(mapping)
    mapping["a.parquet"] = hashlib.sha256(b"two").hexdigest()
    second = area_histogram_input_sha256(mapping)
    assert first != second


def test_area_histogram_input_sha256_changes_with_new_file() -> None:
    first = area_histogram_input_sha256({"a.parquet": "x"})
    second = area_histogram_input_sha256({"a.parquet": "x", "b.parquet": "y"})
    assert first != second


def test_area_histogram_input_sha256_is_stable_under_key_reordering() -> None:
    a = area_histogram_input_sha256({"a.parquet": "x", "b.parquet": "y"})
    b = area_histogram_input_sha256({"b.parquet": "y", "a.parquet": "x"})
    assert a == b


# --- rendering -----------------------------------------------------------


def test_render_area_histogram_writes_png(tmp_path: Path) -> None:
    counts = {label: 1 for label in AREA_BUCKET_LABELS}
    out = tmp_path / "hist.png"
    result = render_area_histogram(counts, out)
    assert isinstance(result, AreaHistogramResult)
    assert result.output_path == out
    assert out.is_file()
    # PNG signature.
    assert out.read_bytes()[:8] == b"\x89PNG\r\n\x1a\n"


def test_render_area_histogram_caption_reports_totals(tmp_path: Path) -> None:
    counts = {
        label: (1 if label in {"1k-10k m²", "1M-10M m²"} else 0) for label in AREA_BUCKET_LABELS
    }
    result = render_area_histogram(counts, tmp_path / "hist.png")
    assert "2 polygons" in result.caption
    assert "2 of 13" in result.caption
    assert "logarithmic" in result.caption.lower()


def test_render_area_histogram_caption_for_empty_dataset(tmp_path: Path) -> None:
    counts = {label: 0 for label in AREA_BUCKET_LABELS}
    result = render_area_histogram(counts, tmp_path / "hist.png")
    assert "0 polygons" in result.caption
    assert "no data" in result.caption.lower()


def test_render_area_histogram_is_byte_stable(tmp_path: Path) -> None:
    counts = {label: index + 1 for index, label in enumerate(AREA_BUCKET_LABELS)}
    a = tmp_path / "a.png"
    b = tmp_path / "b.png"
    render_area_histogram(counts, a)
    render_area_histogram(counts, b)
    assert a.read_bytes() == b.read_bytes()


def test_render_area_histogram_preserves_mtime_on_byte_identical_re_render(
    tmp_path: Path,
) -> None:
    counts = {label: index + 1 for index, label in enumerate(AREA_BUCKET_LABELS)}
    out = tmp_path / "hist.png"
    render_area_histogram(counts, out)
    mtime = out.stat().st_mtime_ns
    time.sleep(0.05)
    render_area_histogram(counts, out)
    assert out.stat().st_mtime_ns == mtime


def test_render_area_histogram_atomic_cleanup_on_failure(tmp_path: Path) -> None:
    """A failure inside the renderer cleans up the temporary file."""
    real_atomic = _atomic_save_png

    def failing_atomic(_fig: object, target: Path) -> None:
        raise RuntimeError("simulated failure")

    import osm_polygon_description_tag.dataset.geography.area_rendering as rendering_module

    rendering_module._atomic_save_png = failing_atomic  # type: ignore[assignment]
    try:
        counts = {label: 0 for label in AREA_BUCKET_LABELS}
        out = tmp_path / "hist.png"
        with pytest.raises(RuntimeError, match="simulated failure"):
            render_area_histogram(counts, out)
    finally:
        rendering_module._atomic_save_png = real_atomic  # type: ignore[assignment]
    assert not (tmp_path / "hist.png").exists()
    leftovers = [
        p for p in tmp_path.iterdir() if p.name.startswith(".hist.png.") and p.name.endswith(".tmp")
    ]
    assert leftovers == []


def test_render_area_histogram_constant_exposed_for_testing(tmp_path: Path) -> None:
    """The test-only re-export of ``_atomic_save_png`` is wired up."""
    import matplotlib.pyplot as plt

    fig, ax = plt.subplots()
    ax.plot([0, 1], [0, 1])
    target = tmp_path / "tiny.png"
    atomic_save_png_for_testing(fig, target)
    plt.close(fig)
    assert target.is_file()


def test_render_area_histogram_byte_stable_for_empty_dataset(tmp_path: Path) -> None:
    """An empty dataset still produces a byte-identical no-data PNG."""
    counts = {label: 0 for label in AREA_BUCKET_LABELS}
    a = tmp_path / "a.png"
    b = tmp_path / "b.png"
    render_area_histogram(counts, a)
    render_area_histogram(counts, b)
    assert a.read_bytes() == b.read_bytes()


# --- reporting integration ----------------------------------------------


def test_generate_dataset_docs_writes_area_histogram_png(tmp_path: Path) -> None:
    data_root = tmp_path / "generated"
    source_root = tmp_path / "raw"
    source_root.mkdir()
    _populate_dataset(data_root, source_root)
    template_path = Path("docs/dataset-card-template.md")

    stats = generate_dataset_docs(data_root, template_path, clock=_frozen_clock)

    histogram_path = data_root / "assets" / "area_distribution.png"
    assert histogram_path.is_file()
    assert histogram_path.read_bytes()[:8] == b"\x89PNG\r\n\x1a\n"
    assert "area_histogram_input_sha256" in stats
    assert stats["area_histogram_render_version"] == AREA_HISTOGRAM_RENDER_VERSION
    assert stats["area_histogram_total_rows"] == stats["rows"]

    readme = (data_root / "README.md").read_text(encoding="utf-8")
    assert "assets/area_distribution.png" in readme
    assert "### Area distribution" in readme


def test_generate_dataset_docs_reuses_histogram_when_unchanged(tmp_path: Path) -> None:
    data_root = tmp_path / "generated"
    source_root = tmp_path / "raw"
    source_root.mkdir()
    _populate_dataset(data_root, source_root)
    template_path = Path("docs/dataset-card-template.md")

    generate_dataset_docs(data_root, template_path, clock=_frozen_clock)
    histogram_path = data_root / "assets" / "area_distribution.png"
    mtime = histogram_path.stat().st_mtime_ns
    size = histogram_path.stat().st_size
    time.sleep(0.05)

    generate_dataset_docs(data_root, template_path, clock=_frozen_clock)

    assert histogram_path.stat().st_mtime_ns == mtime
    assert histogram_path.stat().st_size == size


def test_generate_dataset_docs_recomputes_histogram_when_parquet_changes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A change to the finalized Parquet bytes invalidates the histogram cache."""
    data_root = tmp_path / "generated"
    source_root = tmp_path / "raw"
    source_root.mkdir()
    _populate_dataset(data_root, source_root)
    template_path = Path("docs/dataset-card-template.md")

    import osm_polygon_description_tag.dataset.reporting as reporting

    aggregate_calls: list[Path] = []
    render_calls: list[Path] = []
    identity_iter = iter(["identity-v1", "identity-v2"])

    def fake_aggregate(root: Path) -> dict[str, int]:
        aggregate_calls.append(root)
        return dict.fromkeys(AREA_BUCKET_LABELS, 1)

    def fake_render(_counts: dict[str, int], output_path: Path) -> None:
        render_calls.append(output_path)
        output_path.parent.mkdir(parents=True, exist_ok=True)
        output_path.write_bytes(f"hist-{len(render_calls)}".encode())

    def fake_histogram_input_sha256(_stats: object) -> str:
        return next(identity_iter)

    monkeypatch.setattr(reporting, "aggregate_area_histogram", fake_aggregate)
    monkeypatch.setattr(reporting, "render_area_histogram", fake_render)
    monkeypatch.setattr(reporting, "_area_histogram_input_sha256", fake_histogram_input_sha256)

    generate_dataset_docs(data_root, template_path, clock=_frozen_clock)
    histogram_path = data_root / "assets" / "area_distribution.png"
    first_bytes = histogram_path.read_bytes()
    assert first_bytes == b"hist-1"

    # Simulate finalized Parquet bytes changing: the next identity differs
    # from the cached identity, so the PNG must be regenerated.
    generate_dataset_docs(data_root, template_path, clock=_frozen_clock)

    assert aggregate_calls == [data_root, data_root]
    assert len(render_calls) == 2
    assert histogram_path.read_bytes() == b"hist-2"
    assert histogram_path.read_bytes() != first_bytes


def test_area_histogram_input_sha256_in_reporting_reflects_stats(tmp_path: Path) -> None:
    data_root = tmp_path / "generated"
    source_root = tmp_path / "raw"
    source_root.mkdir()
    _populate_dataset(data_root, source_root)
    template_path = Path("docs/dataset-card-template.md")

    stats = generate_dataset_docs(data_root, template_path, clock=_frozen_clock)
    expected = area_histogram_input_sha256(
        {entry["parquet"]: entry["output_sha256"] for entry in stats["files"]}
    )
    assert stats["area_histogram_input_sha256"] == expected
    # And the internal helper agrees.
    assert _area_histogram_input_sha256(stats) == expected
