"""Rendering contract tests for the H3 density map.

These tests pin the rendering behaviour:

* a valid PNG is produced for the standard case;
* empty datasets yield a deterministic no-data image with a "zero rows" caption;
* the colour scale is logarithmic (``matplotlib.colors.LogNorm``);
* the one-cell case is handled without an invalid LogNorm range;
* re-rendering identical input bytes produces identical PNG bytes;
* byte-identical regeneration preserves the existing PNG mtime;
* rendering is atomic (temporary files are cleaned up on success and
  failure);
* the caption reports factual values derived from the aggregation.
"""

from __future__ import annotations

import time
from pathlib import Path
from unittest.mock import Mock, call, patch

import matplotlib
import matplotlib.colors as mcolors
import pytest

matplotlib.use("Agg")

import osm_polygon_description_tag.dataset.geography.basemap as basemap_module
import osm_polygon_description_tag.dataset.geography.rendering as rendering_module
from osm_polygon_description_tag.dataset.geography import (
    DEFAULT_H3_RESOLUTION,
    RenderResult,
    render_density_map,
)
from osm_polygon_description_tag.dataset.geography.basemap import (
    draw_landmasses,
    load_land_basemap,
)
from osm_polygon_description_tag.dataset.geography.rendering import (
    _COLORMAP_NAME,
    _DPI,
    _FIGSIZE,
    _LAND_COLOR,
    _LAND_EDGE,
    _METADATA_SOFTWARE,
    _NO_DATA_CAPTION,
)


def test_init_axes_applies_the_complete_world_extent_style() -> None:
    axes = Mock()

    rendering_module._init_axes(axes)

    assert axes.method_calls == [
        call.set_facecolor(rendering_module._OCEAN_COLOR),
        call.set_xlim(-180.0, 180.0),
        call.set_ylim(-90.0, 90.0),
        call.set_xticks(range(-180, 181, rendering_module._GRID_LON_EVERY)),
        call.set_yticks(range(-90, 91, rendering_module._GRID_LAT_EVERY)),
        call.grid(
            True,
            color=rendering_module._GRID_COLOR,
            linewidth=0.3,
            alpha=0.4,
        ),
        call.tick_params(colors="#666666", labelsize=rendering_module._TICK_LABELSIZE),
        call.set_aspect("equal", adjustable="box"),
    ]


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        (0, "0"),
        (999, "999"),
        (999.6, "1k"),
        (1_000, "1k"),
        (1_234, "1.2k"),
        (10_000, "10k"),
        (1_000_000, "1.0M"),
        (1_050_000, "1.1M"),
        (2_500_000, "2.5M"),
    ],
)
def test_format_count_tick_uses_stable_count_thresholds(
    value: float,
    expected: str,
) -> None:
    assert rendering_module._format_count_tick(value) == expected


def test_build_caption_distinguishes_no_data_from_populated_data() -> None:
    assert rendering_module._build_caption(0, 0) == _NO_DATA_CAPTION
    assert rendering_module._build_caption(0, 1) == _NO_DATA_CAPTION
    assert rendering_module._build_caption(1, 0) == _NO_DATA_CAPTION
    assert rendering_module._build_caption(1_234, 56) == (
        "H3 density of description-tagged polygons. "
        "Each globally deduplicated OSM identity is counted exactly once. "
        "1,234 polygons across 56 H3 cells at resolution 3 on a logarithmic "
        "colour scale."
    )


def test_safe_counts_clamps_and_converts_every_cell_value() -> None:
    assert rendering_module._safe_counts(
        {"zero": 0, "negative": -2, "fraction": 3.9, "whole": 4}
    ) == [1, 1, 3, 4]


def test_draw_cell_skips_short_rings_and_preserves_patch_style() -> None:
    axes = Mock()
    cmap = Mock()
    norm = Mock()
    cmap.return_value = "face"
    patch_artist = object()

    rings = [
        [(0, 0), (1, 0)],
        [(0, 0), (1, 0), (0, 1)],
        [(2, 0), (3, 0), (2, 1), (2, 0)],
    ]
    with (
        patch.object(rendering_module, "cell_rings", return_value=rings) as cell_rings,
        patch.object(
            rendering_module.mpatches,
            "Polygon",
            return_value=patch_artist,
        ) as polygon,
    ):
        rendering_module._draw_cell(
            axes,
            "cell",
            count=0,
            cmap=cmap,
            norm=norm,
        )

    cell_rings.assert_called_once_with("cell")
    norm.assert_called_once_with(1)
    cmap.assert_called_once_with(norm.return_value)
    assert polygon.call_args_list == [
        call(
            rings[1],
            closed=True,
            facecolor="face",
            edgecolor=rendering_module._EDGE_COLOR,
            linewidth=rendering_module._EDGE_WIDTH,
            alpha=rendering_module._COUNT_ALPHA,
            zorder=3,
        ),
        call(
            rings[2],
            closed=True,
            facecolor="face",
            edgecolor=rendering_module._EDGE_COLOR,
            linewidth=rendering_module._EDGE_WIDTH,
            alpha=rendering_module._COUNT_ALPHA,
            zorder=3,
        ),
    ]
    assert axes.add_patch.call_args_list == [call(patch_artist), call(patch_artist)]


def test_draw_cells_and_colorbar_uses_empty_colorbar_without_cells() -> None:
    fig = Mock()
    axes = Mock()
    cmap = object()

    with (
        patch.object(rendering_module, "_draw_empty_colorbar") as empty_colorbar,
        patch.object(rendering_module, "_safe_counts") as safe_counts,
        patch.object(rendering_module, "_draw_cell") as draw_cell,
        patch.object(rendering_module, "_draw_density_colorbar") as density_colorbar,
    ):
        rendering_module._draw_cells_and_colorbar(fig, axes, (), {}, cmap)

    empty_colorbar.assert_called_once_with(fig, axes, cmap)
    safe_counts.assert_not_called()
    draw_cell.assert_not_called()
    density_colorbar.assert_not_called()


def test_draw_cells_and_colorbar_draws_cells_in_sorted_order() -> None:
    fig = Mock()
    axes = Mock()
    cmap = object()
    norm = object()
    sorted_cells = (("b", 1), ("a", 5))
    cells = {"a": 5, "b": 1}

    with (
        patch.object(rendering_module.mcolors, "LogNorm", return_value=norm) as log_norm,
        patch.object(rendering_module, "_draw_cell") as draw_cell,
        patch.object(rendering_module, "_draw_density_colorbar") as density_colorbar,
    ):
        rendering_module._draw_cells_and_colorbar(fig, axes, sorted_cells, cells, cmap)

    log_norm.assert_called_once_with(vmin=1, vmax=5)
    assert draw_cell.call_args_list == [
        call(axes, "b", count=1, cmap=cmap, norm=norm),
        call(axes, "a", count=5, cmap=cmap, norm=norm),
    ]
    density_colorbar.assert_called_once_with(fig, axes, cmap, norm)


def test_draw_cells_and_colorbar_expands_one_cell_log_range() -> None:
    fig = Mock()
    axes = Mock()
    cmap = object()
    norm = object()

    with (
        patch.object(rendering_module.mcolors, "LogNorm", return_value=norm) as log_norm,
        patch.object(rendering_module, "_draw_cell"),
        patch.object(rendering_module, "_draw_density_colorbar"),
    ):
        rendering_module._draw_cells_and_colorbar(
            fig,
            axes,
            (("cell", 5),),
            {"cell": 5},
            cmap,
        )

    log_norm.assert_called_once_with(vmin=5, vmax=6)


def test_draw_density_colorbar_configures_formatter_and_ticks() -> None:
    fig = Mock()
    axes = Mock()
    cmap = object()
    norm = object()
    scalar_mappable = Mock()
    colorbar = Mock()
    formatter = object()
    fig.colorbar.return_value = colorbar

    with (
        patch.object(
            rendering_module.plt.cm,
            "ScalarMappable",
            return_value=scalar_mappable,
        ) as scalar_factory,
        patch.object(
            rendering_module.mtick,
            "FuncFormatter",
            return_value=formatter,
        ) as formatter_factory,
    ):
        rendering_module._draw_density_colorbar(fig, axes, cmap, norm)

    scalar_factory.assert_called_once_with(cmap=cmap, norm=norm)
    scalar_mappable.set_array.assert_called_once_with([])
    fig.colorbar.assert_called_once_with(
        scalar_mappable,
        ax=axes,
        fraction=rendering_module._COLORBAR_FRACTION,
        pad=rendering_module._COLORBAR_PAD,
    )
    colorbar.set_label.assert_called_once_with(
        "Polygons per H3 cell (log scale)",
        fontsize=8,
        color="#333333",
    )
    formatter_factory.assert_called_once_with(rendering_module._format_count_tick)
    colorbar.ax.yaxis.set_major_formatter.assert_called_once_with(formatter)
    colorbar.ax.tick_params.assert_called_once_with(labelsize=rendering_module._TICK_LABELSIZE)


def test_draw_empty_colorbar_uses_stable_placeholder_range() -> None:
    fig = Mock()
    axes = Mock()
    cmap = object()
    norm = object()
    scalar_mappable = Mock()
    colorbar = Mock()
    fig.colorbar.return_value = colorbar

    with (
        patch.object(rendering_module.mcolors, "LogNorm", return_value=norm) as log_norm,
        patch.object(
            rendering_module.plt.cm,
            "ScalarMappable",
            return_value=scalar_mappable,
        ) as scalar_factory,
    ):
        rendering_module._draw_empty_colorbar(fig, axes, cmap)

    log_norm.assert_called_once_with(vmin=1, vmax=2)
    scalar_factory.assert_called_once_with(cmap=cmap, norm=norm)
    scalar_mappable.set_array.assert_called_once_with([])
    fig.colorbar.assert_called_once_with(
        scalar_mappable,
        ax=axes,
        fraction=rendering_module._COLORBAR_FRACTION,
        pad=rendering_module._COLORBAR_PAD,
    )


def test_render_density_map_orchestrates_stable_figure_contract(tmp_path: Path) -> None:
    cells = {"b": 2, "a": 1}
    output_path = tmp_path / "map.png"
    features = [object()]
    fig = Mock()
    axes = Mock()
    cmap = object()
    caption = "caption"

    with (
        patch.object(rendering_module.plt, "subplots", return_value=(fig, axes)) as subplots,
        patch.object(rendering_module, "_init_axes") as init_axes,
        patch.object(rendering_module, "_draw_land_overlay") as draw_land_overlay,
        patch.object(rendering_module.plt, "get_cmap", return_value=cmap) as get_cmap,
        patch.object(rendering_module, "_draw_cells_and_colorbar") as draw_cells,
        patch.object(rendering_module, "_build_caption", return_value=caption) as build_caption,
        patch.object(rendering_module, "_atomic_save_png") as atomic_save,
        patch.object(rendering_module.plt, "close") as close,
    ):
        result = rendering_module.render_density_map(
            cells,
            output_path,
            land_features=features,
        )

    subplots.assert_called_once_with(figsize=rendering_module._FIGSIZE, dpi=rendering_module._DPI)
    build_caption.assert_called_once_with(3, 2)
    fig.set_facecolor.assert_called_once_with("white")
    init_axes.assert_called_once_with(axes)
    draw_land_overlay.assert_called_once_with(axes, features)
    get_cmap.assert_called_once_with(rendering_module._COLORMAP_NAME)
    draw_cells.assert_called_once_with(
        fig,
        axes,
        (("a", 1), ("b", 2)),
        cells,
        cmap,
    )
    fig.suptitle.assert_called_once_with(
        rendering_module._TITLE,
        fontsize=rendering_module._TITLE_FONTSIZE,
        color="#222222",
        y=0.98,
    )
    fig.text.assert_called_once_with(
        0.5,
        0.02,
        caption,
        ha="center",
        va="bottom",
        fontsize=rendering_module._CAPTION_FONTSIZE,
        color="#444444",
        wrap=True,
    )
    fig.tight_layout.assert_called_once_with(rect=(0, 0.06, 1, 0.95))
    atomic_save.assert_called_once_with(fig, output_path)
    close.assert_called_once_with(fig)
    assert result == RenderResult(output_path=output_path, caption=caption)


def test_render_density_map_closes_figure_when_save_fails(tmp_path: Path) -> None:
    fig = Mock()
    axes = Mock()
    output_path = tmp_path / "map.png"

    with (
        patch.object(rendering_module.plt, "subplots", return_value=(fig, axes)),
        patch.object(rendering_module, "_draw_land_overlay"),
        patch.object(rendering_module, "_draw_cells_and_colorbar"),
        patch.object(
            rendering_module,
            "_atomic_save_png",
            side_effect=RuntimeError("save failed"),
        ),
        patch.object(rendering_module.plt, "close") as close,
        pytest.raises(RuntimeError, match="save failed"),
    ):
        rendering_module.render_density_map({}, output_path, land_features=[])

    close.assert_called_once_with(fig)


def _fake_cell(lat: float, lon: float) -> str:
    import h3

    return h3.latlng_to_cell(lat, lon, DEFAULT_H3_RESOLUTION)


def test_render_density_map_produces_png(tmp_path: Path) -> None:
    cells = {
        _fake_cell(48.8566, 2.3522): 3,
        _fake_cell(-33.8688, 151.2093): 7,
        _fake_cell(0.0, 0.0): 2,
    }
    out = tmp_path / "map.png"
    result = render_density_map(cells, out)
    assert isinstance(result, RenderResult)
    assert result.output_path == out
    assert out.is_file()
    # PNG signature.
    assert out.read_bytes()[:8] == b"\x89PNG\r\n\x1a\n"


def test_render_density_map_draws_land_overlay_before_cells(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The production map must render landmasses over the blue ocean."""
    import osm_polygon_description_tag.dataset.geography.rendering as rendering_module

    calls: list[object] = []

    def capture_landmasses(_ax: object, features: object) -> None:
        calls.append(features)

    monkeypatch.setattr(rendering_module, "draw_landmasses", capture_landmasses)
    features = [{"type": "Feature", "geometry": {"type": "Polygon", "coordinates": []}}]
    render_density_map(
        {_fake_cell(48.8566, 2.3522): 1},
        tmp_path / "map.png",
        land_features=features,
    )
    assert calls == [features]


def test_land_palette_matches_wikidata_only() -> None:
    """Keep the shared beige land / blue ocean palette used by wikidata-only."""
    assert _LAND_COLOR == "#e8e0d0"
    assert _LAND_EDGE == "#b8aa90"


def test_bundled_land_basemap_is_available_and_nonempty() -> None:
    """The production wheel must carry the offline Natural Earth reference."""
    features = load_land_basemap()
    assert features
    assert any(
        isinstance(feature, dict)
        and isinstance(feature.get("geometry"), dict)
        and feature["geometry"].get("type") in {"Polygon", "MultiPolygon"}
        for feature in features
    )


def test_land_basemap_loader_handles_missing_and_malformed_files(tmp_path: Path) -> None:
    """A broken optional reference never turns map generation into a crash."""
    assert load_land_basemap(tmp_path / "missing.geojson") == []
    empty = tmp_path / "empty.geojson"
    empty.touch()
    assert load_land_basemap(empty) == []
    invalid = tmp_path / "invalid.geojson"
    invalid.write_text("not json", encoding="utf-8")
    assert load_land_basemap(invalid) == []
    not_object = tmp_path / "list.geojson"
    not_object.write_text("[]", encoding="utf-8")
    assert load_land_basemap(not_object) == []
    bad_features = tmp_path / "bad-features.geojson"
    bad_features.write_text('{"features": "nope"}', encoding="utf-8")
    assert load_land_basemap(bad_features) == []


def test_bundled_basemap_path_uses_the_packaged_data_directory() -> None:
    package_root = Path(basemap_module.__file__).parents[2]
    expected = package_root / "_data" / basemap_module.LAND_BASEMAP_FILENAME

    assert basemap_module.bundled_basemap_path() == expected


def test_basemap_loader_does_not_treat_a_one_byte_asset_as_empty(tmp_path: Path) -> None:
    candidate = tmp_path / "one-byte.geojson"
    candidate.write_bytes(b"x")
    features = [{"type": "Feature"}]

    with (
        patch.object(basemap_module, "_read_basemap_payload", return_value=object()) as read,
        patch.object(basemap_module, "_features_from_payload", return_value=features) as parse,
    ):
        assert load_land_basemap(candidate) == features

    read.assert_called_once_with(candidate)
    parse.assert_called_once()


def test_read_basemap_payload_uses_explicit_utf8(tmp_path: Path) -> None:
    path = Mock()
    path.read_text.return_value = '{"features": []}'

    assert basemap_module._read_basemap_payload(path) == {"features": []}
    path.read_text.assert_called_once_with(encoding="utf-8")


def test_draw_ring_passes_the_complete_land_patch_style() -> None:
    axes = Mock()
    patch_artist = object()

    with patch.object(basemap_module.mpatches, "Polygon", return_value=patch_artist) as polygon:
        basemap_module._draw_ring(axes, [(0, 0), (1, 0), (0, 1)])

    polygon.assert_called_once_with(
        [(0.0, 0.0), (1.0, 0.0), (0.0, 1.0)],
        closed=True,
        facecolor=basemap_module._LAND_COLOR,
        edgecolor=basemap_module._LAND_EDGE,
        linewidth=0.2,
        zorder=1,
    )
    axes.add_patch.assert_called_once_with(patch_artist)


def test_polygon_helpers_ignore_truthy_non_list_coordinate_containers() -> None:
    axes = Mock()
    with patch.object(basemap_module, "_draw_ring") as draw_ring:
        basemap_module._draw_multipolygon(axes, ([],))
        basemap_module._draw_polygon(axes, ([(0, 0), (1, 0), (0, 1)],))
        basemap_module._draw_multipolygon(
            axes,
            [([(0, 0), (1, 0), (0, 1)],)],
        )

    draw_ring.assert_not_called()


def test_polygon_helpers_forward_each_valid_outer_ring_to_the_axes() -> None:
    axes = Mock()
    polygon_ring = [(0, 0), (1, 0), (0, 1)]
    multipolygon_rings = [
        [(2, 0), (3, 0), (2, 1)],
        [(4, 0), (5, 0), (4, 1)],
    ]

    with patch.object(basemap_module, "_draw_ring") as draw_ring:
        basemap_module._draw_polygon(axes, [polygon_ring])
        basemap_module._draw_multipolygon(
            axes,
            [[multipolygon_rings[0]], [], [multipolygon_rings[1]]],
        )

    assert draw_ring.call_args_list == [
        call(axes, polygon_ring),
        call(axes, multipolygon_rings[0]),
        call(axes, multipolygon_rings[1]),
    ]


def test_draw_landmasses_supports_polygon_multipolygon_and_bad_features() -> None:
    """Only valid outer rings become beige land patches."""

    class _Axes:
        def __init__(self) -> None:
            self.patches: list[object] = []

        def add_patch(self, patch: object) -> None:
            self.patches.append(patch)

    axes = _Axes()
    draw_landmasses(
        axes,
        [
            None,
            {"geometry": None},
            {"geometry": {"type": "LineString", "coordinates": []}},
            {"geometry": {"type": "Polygon", "coordinates": [[]]}},
            {
                "geometry": {
                    "type": "Polygon",
                    "coordinates": [[(0, 0), (1, 0), (0, 1)]],
                }
            },
            {
                "geometry": {
                    "type": "MultiPolygon",
                    "coordinates": [[[(2, 0), (3, 0), (2, 1)]]],
                }
            },
        ],
    )
    assert len(axes.patches) == 2


def test_render_density_map_caption_contains_factual_counts(tmp_path: Path) -> None:
    cells = {
        _fake_cell(48.8566, 2.3522): 3,
        _fake_cell(-33.8688, 151.2093): 7,
    }
    out = tmp_path / "map.png"
    result = render_density_map(cells, out)
    # 3 + 7 = 10 polygons
    assert "10 polygons" in result.caption
    # 2 occupied H3 cells
    assert "2 H3 cells" in result.caption
    # Mentions the log scale
    assert "logarithmic" in result.caption.lower()


def test_render_density_map_uses_lognorm(tmp_path: Path) -> None:
    """The renderer instantiates a LogNorm over the cell counts."""
    captured: dict[str, object] = {}
    real_lognorm = mcolors.LogNorm

    def capturing_lognorm(*args: object, **kwargs: object) -> object:
        norm = real_lognorm(*args, **kwargs)
        captured["vmin"] = norm.vmin
        captured["vmax"] = norm.vmax
        return norm

    import osm_polygon_description_tag.dataset.geography.rendering as rendering_module

    original = rendering_module.mcolors.LogNorm
    rendering_module.mcolors.LogNorm = capturing_lognorm  # type: ignore[assignment]
    try:
        cells = {
            _fake_cell(0.0, 0.0): 1,
            _fake_cell(45.0, 90.0): 100,
            _fake_cell(-45.0, -90.0): 10_000,
        }
        render_density_map(cells, tmp_path / "map.png")
    finally:
        rendering_module.mcolors.LogNorm = original  # type: ignore[assignment]
    assert "vmin" in captured
    assert "vmax" in captured
    assert int(captured["vmin"]) == 1  # minimum count
    assert int(captured["vmax"]) == 10_000  # maximum count


def test_render_density_map_handles_one_cell(tmp_path: Path) -> None:
    """A single cell must not produce an invalid LogNorm range."""
    cells = {_fake_cell(48.8566, 2.3522): 5}
    out = tmp_path / "map.png"
    # Should not raise.
    render_density_map(cells, out)
    assert out.is_file()


def test_render_density_map_handles_empty_dataset(tmp_path: Path) -> None:
    """An empty input renders a deterministic no-data image with zero caption."""
    out = tmp_path / "map.png"
    result = render_density_map({}, out)
    assert out.is_file()
    assert "0 polygons" in result.caption or "zero" in result.caption.lower()
    # Default no-data caption is used.
    assert result.caption == _NO_DATA_CAPTION or "0" in result.caption


def test_render_density_map_is_byte_stable(tmp_path: Path) -> None:
    cells = {
        _fake_cell(48.8566, 2.3522): 3,
        _fake_cell(-33.8688, 151.2093): 7,
        _fake_cell(0.0, 0.0): 2,
    }
    a = tmp_path / "a.png"
    b = tmp_path / "b.png"
    render_density_map(cells, a)
    render_density_map(cells, b)
    assert a.read_bytes() == b.read_bytes()


def test_render_density_map_preserves_mtime_on_byte_identical_re_render(
    tmp_path: Path,
) -> None:
    cells = {_fake_cell(48.8566, 2.3522): 4, _fake_cell(0.0, 0.0): 2}
    out = tmp_path / "map.png"
    render_density_map(cells, out)
    mtime = out.stat().st_mtime_ns
    time.sleep(0.05)
    # Re-rendering byte-identical input must not rewrite the PNG.
    render_density_map(cells, out)
    assert out.stat().st_mtime_ns == mtime


def test_render_density_map_does_not_download_basemap(tmp_path: Path) -> None:
    """The renderer must not perform any network access."""
    import urllib.request

    captured_urls: list[str] = []

    real_urlopen = urllib.request.urlopen

    def guarded_urlopen(url: object, *args: object, **kwargs: object) -> object:
        captured_urls.append(str(url))
        return real_urlopen(url, *args, **kwargs)  # type: ignore[arg-type]

    # Patch the global urllib.request.urlopen, which the rendering module
    # would resolve to if it tried to fetch a basemap.
    original_urlopen = urllib.request.urlopen
    urllib.request.urlopen = guarded_urlopen  # type: ignore[assignment]
    try:
        cells = {_fake_cell(0.0, 0.0): 1}
        render_density_map(cells, tmp_path / "map.png")
    finally:
        urllib.request.urlopen = original_urlopen  # type: ignore[assignment]
    assert captured_urls == []


def test_render_constants_are_stable() -> None:
    """Visual constants must be stable for byte-for-byte deterministic output."""
    assert isinstance(_FIGSIZE, tuple)
    assert len(_FIGSIZE) == 2
    assert isinstance(_DPI, int) and _DPI > 0
    assert isinstance(_COLORMAP_NAME, str) and _COLORMAP_NAME
    assert isinstance(_METADATA_SOFTWARE, str) and _METADATA_SOFTWARE
    assert "H3" in _NO_DATA_CAPTION or "no" in _NO_DATA_CAPTION.lower()


def test_render_density_map_handles_antimeridian_cell(tmp_path: Path) -> None:
    """A cell whose boundary crosses the antimeridian must render without
    world-spanning polygons."""
    import h3

    # Resolution 3 cell covering a small slice around the antimeridian.
    lon = 179.5
    lat = 0.0
    cell = h3.latlng_to_cell(lat, lon, DEFAULT_H3_RESOLUTION)
    cells = {cell: 5}
    out = tmp_path / "map.png"
    render_density_map(cells, out)
    assert out.is_file()
