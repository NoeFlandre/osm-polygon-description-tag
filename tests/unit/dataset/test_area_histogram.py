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
from unittest.mock import Mock, call, patch

import matplotlib
import pyarrow as pa
import pyarrow.parquet as pq
import pytest
from shapely.geometry import Polygon

matplotlib.use("Agg")

import osm_polygon_description_tag.dataset.geography.area_histogram as area_histogram_module
import osm_polygon_description_tag.dataset.geography.area_rendering as area_rendering_module
from osm_polygon_description_tag.dataset.docs import (
    _area_histogram_input_sha256,
    generate_dataset_docs,
)
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
    _format_count_tick,
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
from osm_polygon_description_tag.dataset.storage import StorageError, write_geoparquet
from tests.conftest import make_record_dict
from tests.helpers.dataset import frozen_clock as _frozen_clock
from tests.helpers.dataset import write_reporting_fixture as _populate_dataset


def _write_parquet_with_areas(directory: Path, name: str, areas: list[float]) -> Path:
    """Write schema-valid GeoParquet rows with controlled area values."""
    records: list[dict[str, object]] = []
    for index, area in enumerate(areas, start=1):
        record = make_record_dict(
            Polygon([(0, 0), (0, 1), (1, 1), (1, 0)]),
            {"description": "area"},
            osm_id=index,
            source_pbf=f"{name}.osm.pbf",
        )
        record["area_m2"] = area
        records.append(record)
    target = directory / f"{name}.parquet"
    write_geoparquet(iter(records), target)
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
                schema_version=3,
                geoparquet_version="1.1.0",
                transform_algorithm_version=3,
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


def test_top_edge_uses_inclusive_last_bucket_label() -> None:
    """The final bucket label describes values at the inclusive top edge."""
    assert AREA_BUCKET_LABELS[-1] == ">=100B m²"


def test_format_count_tick_uses_human_scale_units() -> None:
    assert _format_count_tick(12.0) == "12"
    assert _format_count_tick(1_200.0) == "1.2k"
    assert _format_count_tick(2_000.0) == "2k"
    assert _format_count_tick(1_500_000.0) == "1.5M"


def test_bucket_index_handles_negative_values() -> None:
    """Negative areas clamp to the smallest bucket (defensive: dataset rejects these)."""
    assert _bucket_index(-1.0) == 0


def test_bucket_index_handles_zero() -> None:
    assert _bucket_index(0.0) == 0


@pytest.mark.parametrize(
    ("area_m2", "expected_index"),
    [
        (1.5, 1),
        (50.0, 2),
        (500.0, 3),
        (5_000.0, 4),
        (50_000.0, 5),
        (500_000.0, 6),
        (5_000_000.0, 7),
        (50_000_000.0, 8),
        (500_000_000.0, 9),
        (5_000_000_000.0, 10),
        (50_000_000_000.0, 11),
    ],
)
def test_bucket_index_finds_interior_values(area_m2: float, expected_index: int) -> None:
    assert _bucket_index(area_m2) == expected_index


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
    # value above the top edge for the open-ended ``>=100B m²`` bucket.
    areas = [0.5, *AREA_BUCKET_EDGES[1:-1], AREA_BUCKET_EDGES[-1] * 10.0]
    data_root = _make_finalized_area_histogram_data_root(tmp_path, {"regions": areas})
    counts = aggregate_area_histogram(data_root)
    assert all(counts[label] == 1 for label in AREA_BUCKET_LABELS)
    assert sum(counts.values()) == len(areas)


def test_aggregate_area_histogram_handles_large_areas(tmp_path: Path) -> None:
    data_root = _make_finalized_area_histogram_data_root(tmp_path, {"big": [3.5e12, 1.2e10, 0.5]})
    counts = aggregate_area_histogram(data_root)
    assert counts["<1 m²"] == 1
    assert counts[">=100B m²"] == 1
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
        tmp_path, {"x": [0.5, 1.0, 10.0, 100.0, 1_000.0]}
    )
    first = aggregate_area_histogram(data_root)
    second = aggregate_area_histogram(data_root)
    assert first == second
    # Also stable as JSON, not just dict equality.
    assert json.dumps(first, sort_keys=True) == json.dumps(second, sort_keys=True)


@pytest.mark.parametrize("batch_size", [None, 17])
def test_aggregate_area_histogram_reads_only_area_column_with_requested_batch_size(
    tmp_path: Path, batch_size: int | None
) -> None:
    data_root = tmp_path / "generated"
    parquet_path = data_root / "data" / "region.parquet"
    batch = pa.record_batch([pa.array([1.0])], names=["area_m2"])
    reader = Mock()
    reader.iter_batches.return_value = [batch]
    seen_directory: list[Path] = []

    def sorted_inputs(directory: Path) -> list[Path]:
        seen_directory.append(directory)
        return [parquet_path]

    with (
        patch("osm_polygon_description_tag.dataset.storage.validate_finalized_artifacts_strict"),
        patch.object(area_histogram_module, "sorted_parquets", side_effect=sorted_inputs),
        patch.object(area_histogram_module.pq, "ParquetFile", return_value=reader),
    ):
        kwargs = {} if batch_size is None else {"batch_size": batch_size}
        counts = aggregate_area_histogram(data_root, **kwargs)

    assert seen_directory == [data_root / "data"]
    expected_batch_size = 8192 if batch_size is None else batch_size
    reader.iter_batches.assert_called_once_with(
        columns=("area_m2",), batch_size=expected_batch_size
    )
    assert counts["1-10 m²"] == 1


def test_aggregate_area_histogram_skips_null_values_in_a_streamed_batch(
    tmp_path: Path,
) -> None:
    data_root = tmp_path / "generated"
    parquet_path = data_root / "data" / "region.parquet"
    batch = pa.record_batch([pa.array([None, 1.0], type=pa.float64())], names=["area_m2"])
    reader = Mock()
    reader.iter_batches.return_value = [batch]

    with (
        patch("osm_polygon_description_tag.dataset.storage.validate_finalized_artifacts_strict"),
        patch.object(area_histogram_module, "sorted_parquets", return_value=[parquet_path]),
        patch.object(area_histogram_module.pq, "ParquetFile", return_value=reader),
    ):
        counts = aggregate_area_histogram(data_root)

    assert sum(counts.values()) == 1
    assert counts["1-10 m²"] == 1


def test_aggregate_area_histogram_rejects_invalid_area_rows(tmp_path: Path) -> None:
    """Strict validation rejects null ``area_m2`` rows before bucketing."""
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
            schema_version=3,
            geoparquet_version="1.1.0",
            transform_algorithm_version=3,
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
    with pytest.raises(StorageError):
        aggregate_area_histogram(data_root)


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


def test_area_histogram_input_sha256_uses_canonical_utf8_json() -> None:
    mapping = {"é.parquet": "sha-é"}
    payload = {
        "cache_schema_version": 1,
        "render_version": AREA_HISTOGRAM_RENDER_VERSION,
        "files": [{"parquet": "é.parquet", "output_sha256": "sha-é"}],
    }
    canonical = json.dumps(
        payload,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    assert area_histogram_input_sha256(mapping) == hashlib.sha256(canonical).hexdigest()


# --- rendering -----------------------------------------------------------


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        (999, "999"),
        (1_000, "1k"),
        (1_234, "1.2k"),
        (1_000_000, "1.0M"),
        (1_050_000, "1.1M"),
        (2_500_000, "2.5M"),
    ],
)
def test_area_format_count_tick_pins_threshold_and_rounding_contract(
    value: float,
    expected: str,
) -> None:
    assert area_rendering_module._format_count_tick(value) == expected


def test_area_build_caption_and_bar_labels_are_exact_and_aligned() -> None:
    counts = {
        AREA_BUCKET_LABELS[0]: 2,
        AREA_BUCKET_LABELS[-1]: 3,
    }

    assert area_rendering_module._build_caption(counts) == (
        "Area distribution of description-tagged polygons. "
        "5 polygons bucketed into 2 of 2 logarithmic area bins (m²)."
    )
    assert area_rendering_module._build_caption({}) == area_rendering_module._NO_DATA_CAPTION

    labels, values = area_rendering_module._bar_labels(counts)
    assert labels == list(AREA_BUCKET_LABELS)
    assert values == [2, *([0] * (len(AREA_BUCKET_LABELS) - 2)), 3]


def test_area_caption_counts_zero_and_one_as_unoccupied_and_occupied_bins() -> None:
    counts = {"empty": 0, "single": 1, "multiple": 2}

    assert area_rendering_module._build_caption(counts) == (
        "Area distribution of description-tagged polygons. "
        "3 polygons bucketed into 2 of 3 logarithmic area bins (m²)."
    )


def test_style_area_figure_sets_stable_background_colors() -> None:
    fig = Mock()
    axes = Mock()

    area_rendering_module._style_area_figure(fig, axes)

    fig.set_facecolor.assert_called_once_with(area_rendering_module._BG_COLOR)
    axes.set_facecolor.assert_called_once_with(area_rendering_module._PANEL_COLOR)


def test_draw_area_bars_filters_nonpositive_values_and_preserves_style() -> None:
    axes = Mock()

    area_rendering_module._draw_area_bars(axes, (0, 1, 2), [0, 3, -1])

    axes.barh.assert_called_once_with(
        [1],
        [3],
        color=area_rendering_module._BAR_COLOR,
        edgecolor=area_rendering_module._BAR_EDGE,
        linewidth=0.5,
        zorder=2,
    )


def test_draw_area_bars_requires_aligned_positions_and_values() -> None:
    with pytest.raises(ValueError):
        area_rendering_module._draw_area_bars(Mock(), (0, 1), [2])


def test_style_area_axes_applies_logarithmic_axis_contract() -> None:
    axes = Mock()
    labels = ["small", "large"]
    positions = object()

    area_rendering_module._style_area_axes(axes, labels, positions)

    assert axes.method_calls == [
        call.set_yticks(positions),
        call.set_yticklabels(
            labels,
            fontsize=area_rendering_module._TICK_FONTSIZE,
            color=area_rendering_module._TEXT_COLOR,
        ),
        call.invert_yaxis(),
        call.set_xscale("log"),
        call.set_xlabel(
            "Polygons per bucket (log scale)",
            fontsize=area_rendering_module._LABEL_FONTSIZE,
            color=area_rendering_module._TEXT_COLOR,
        ),
        call.tick_params(
            axis="x",
            colors=area_rendering_module._MUTED_COLOR,
            labelsize=area_rendering_module._TICK_FONTSIZE,
        ),
        call.tick_params(axis="y", colors=area_rendering_module._TEXT_COLOR),
    ]


def test_annotate_area_bars_places_stable_labels_and_clamps_zero_width() -> None:
    axes = Mock()

    area_rendering_module._annotate_area_bars(axes, (0, 1), [0, 1_250])

    assert axes.text.call_args_list == [
        call(
            1.08,
            0,
            "0",
            va="center",
            ha="left",
            fontsize=area_rendering_module._TICK_FONTSIZE,
            color=area_rendering_module._TEXT_COLOR,
            zorder=4,
        ),
        call(
            1_250 * 1.08,
            1,
            "1.2k",
            va="center",
            ha="left",
            fontsize=area_rendering_module._TICK_FONTSIZE,
            color=area_rendering_module._TEXT_COLOR,
            zorder=4,
        ),
    ]


def test_annotate_area_bars_requires_aligned_positions_and_values() -> None:
    with pytest.raises(ValueError):
        area_rendering_module._annotate_area_bars(Mock(), (0, 1), [2])


def test_style_area_grid_hides_frame_and_draws_stable_grid() -> None:
    spines = {name: Mock() for name in ("top", "right", "left", "bottom")}
    axes = Mock()
    axes.spines = spines

    area_rendering_module._style_area_grid(axes)

    spines["top"].set_visible.assert_called_once_with(False)
    spines["right"].set_visible.assert_called_once_with(False)
    spines["left"].set_color.assert_called_once_with(area_rendering_module._MUTED_COLOR)
    spines["bottom"].set_color.assert_called_once_with(area_rendering_module._MUTED_COLOR)
    axes.grid.assert_called_once_with(
        True,
        axis="x",
        color="#ffffff",
        linewidth=0.8,
        alpha=0.9,
        zorder=1,
    )
    axes.set_axisbelow.assert_called_once_with(True)
    axes.set_title.assert_called_once_with(
        area_rendering_module._TITLE,
        fontsize=area_rendering_module._TITLE_FONTSIZE,
        color=area_rendering_module._TEXT_COLOR,
        pad=12,
        loc="left",
    )


def test_add_area_caption_uses_stable_layout_and_typography() -> None:
    fig = Mock()

    area_rendering_module._add_area_caption(fig, "caption")

    fig.text.assert_called_once_with(
        0.5,
        0.02,
        "caption",
        ha="center",
        va="bottom",
        fontsize=area_rendering_module._CAPTION_FONTSIZE,
        color=area_rendering_module._MUTED_COLOR,
        wrap=True,
    )


def test_set_area_limits_reserves_annotation_headroom_for_empty_and_populated_data() -> None:
    populated_axes = Mock()
    area_rendering_module._set_area_limits(populated_axes, [2, 100])
    populated_axes.set_xlim.assert_called_once_with(left=1.0, right=400.0)

    empty_axes = Mock()
    area_rendering_module._set_area_limits(empty_axes, [])
    empty_axes.set_xlim.assert_called_once_with(left=1.0, right=10.0)


def test_render_area_histogram_orchestrates_helpers_and_closes_figure(
    tmp_path: Path,
) -> None:
    counts = {"b": 2, "a": 0}
    labels = ["a", "b"]
    values = [0, 2]
    positions = object()
    caption = "caption"
    fig = Mock()
    axes = Mock()
    output_path = tmp_path / "hist.png"
    calls: list[str] = []

    def record(name: str):
        def wrapper(*_args: object, **_kwargs: object) -> None:
            calls.append(name)

        return wrapper

    with (
        patch.object(
            area_rendering_module, "_bar_labels", return_value=(labels, values)
        ) as bar_labels,
        patch.object(
            area_rendering_module, "_build_caption", return_value=caption
        ) as build_caption,
        patch.object(
            area_rendering_module.plt,
            "subplots",
            return_value=(fig, axes),
        ) as subplots,
        patch.object(area_rendering_module.np, "arange", return_value=positions) as arange,
        patch.object(area_rendering_module, "_style_area_figure", side_effect=record("figure")),
        patch.object(area_rendering_module, "_draw_area_bars", side_effect=record("bars")),
        patch.object(area_rendering_module, "_style_area_axes", side_effect=record("axes")),
        patch.object(area_rendering_module, "_annotate_area_bars", side_effect=record("labels")),
        patch.object(area_rendering_module, "_style_area_grid", side_effect=record("grid")),
        patch.object(
            area_rendering_module, "_add_area_caption", side_effect=record("caption")
        ) as add_caption,
        patch.object(
            area_rendering_module, "_set_area_limits", side_effect=record("limits")
        ) as set_limits,
        patch.object(area_rendering_module, "_atomic_save_png") as atomic_save,
        patch.object(area_rendering_module.plt, "close") as close,
    ):
        result = area_rendering_module.render_area_histogram(counts, output_path)

    bar_labels.assert_called_once_with(counts)
    build_caption.assert_called_once_with(counts)
    subplots.assert_called_once_with(
        figsize=area_rendering_module._FIGSIZE,
        dpi=area_rendering_module._DPI,
    )
    arange.assert_called_once_with(2)
    assert calls == ["figure", "bars", "axes", "labels", "grid", "caption", "limits"]
    add_caption.assert_called_once_with(fig, caption)
    set_limits.assert_called_once_with(axes, values)
    fig.tight_layout.assert_called_once_with(rect=(0, 0.05, 1, 0.97))
    atomic_save.assert_called_once_with(fig, output_path)
    close.assert_called_once_with(fig)
    assert result == AreaHistogramResult(output_path=output_path, caption=caption)


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
    from matplotlib.figure import Figure

    figures_before = area_rendering_module.plt.get_fignums()
    out = tmp_path / "hist.png"
    out.write_bytes(b"original")
    with (
        patch.object(Figure, "savefig", side_effect=RuntimeError("simulated failure")),
        pytest.raises(RuntimeError, match="simulated failure"),
    ):
        render_area_histogram({}, out)

    assert out.read_bytes() == b"original"
    assert list(tmp_path.glob(".hist.png.*.tmp")) == []
    assert area_rendering_module.plt.get_fignums() == figures_before


def test_render_area_histogram_byte_stable_for_empty_dataset(tmp_path: Path) -> None:
    """An empty dataset still produces a byte-identical no-data PNG."""
    counts = {label: 0 for label in AREA_BUCKET_LABELS}
    a = tmp_path / "a.png"
    b = tmp_path / "b.png"
    render_area_histogram(counts, a)
    render_area_histogram(counts, b)
    assert a.read_bytes() == b.read_bytes()


def test_render_area_histogram_does_not_draw_zero_count_bars(tmp_path: Path) -> None:
    """Empty bins remain visually empty even though the x-axis is logarithmic."""
    import osm_polygon_description_tag.dataset.geography.area_rendering as rendering

    counts = {label: 0 for label in AREA_BUCKET_LABELS}
    counts[AREA_BUCKET_LABELS[2]] = 4
    captured: list[list[float]] = []
    real_barh = rendering.plt.Axes.barh

    def capture_barh(
        self: object, _positions: object, widths: object, *args: object, **kwargs: object
    ):
        captured.append(list(widths))
        return real_barh(self, _positions, widths, *args, **kwargs)

    monkeypatch = pytest.MonkeyPatch()
    monkeypatch.setattr(rendering.plt.Axes, "barh", capture_barh)
    try:
        render_area_histogram(counts, tmp_path / "hist.png")
    finally:
        monkeypatch.undo()

    assert captured == [[4]]


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


def test_generate_dataset_docs_recomputes_when_render_version_changes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A renderer change invalidates the cached PNG even when Parquet bytes match."""
    data_root = tmp_path / "generated"
    source_root = tmp_path / "raw"
    source_root.mkdir()
    _populate_dataset(data_root, source_root)
    template_path = Path("docs/dataset-card-template.md")

    generate_dataset_docs(data_root, template_path, clock=_frozen_clock)
    stats_path = data_root / "stats.json"
    stats = json.loads(stats_path.read_text(encoding="utf-8"))
    stats["area_histogram_render_version"] = AREA_HISTOGRAM_RENDER_VERSION - 1
    stats_path.write_text(json.dumps(stats), encoding="utf-8")

    calls: list[Path] = []
    monkeypatch.setattr(
        "osm_polygon_description_tag.dataset.docs.render_area_histogram",
        lambda counts, target: calls.append(target),
    )
    generate_dataset_docs(data_root, template_path, clock=_frozen_clock)

    assert calls == [data_root / "assets" / "area_distribution.png"]


def test_generate_dataset_docs_recomputes_histogram_when_parquet_changes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A change to the finalized Parquet bytes invalidates the histogram cache."""
    data_root = tmp_path / "generated"
    source_root = tmp_path / "raw"
    source_root.mkdir()
    _populate_dataset(data_root, source_root)
    template_path = Path("docs/dataset-card-template.md")

    import osm_polygon_description_tag.dataset.docs as reporting

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
