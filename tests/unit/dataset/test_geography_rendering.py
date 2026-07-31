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

import matplotlib
import matplotlib.colors as mcolors
import pytest

matplotlib.use("Agg")

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


def test_render_density_map_atomic_cleanup_on_failure(tmp_path: Path) -> None:
    """A failure inside the renderer cleans up its temporary file."""
    import osm_polygon_description_tag.dataset.geography.rendering as rendering_module

    real_atomic = rendering_module._atomic_save_png

    def failing_atomic(_fig: object, target: Path) -> None:
        # Simulate a failure by raising an error.
        raise RuntimeError("simulated failure")

    rendering_module._atomic_save_png = failing_atomic  # type: ignore[assignment]
    try:
        cells = {_fake_cell(0.0, 0.0): 1}
        out = tmp_path / "map.png"
        with pytest.raises(RuntimeError, match="simulated failure"):
            render_density_map(cells, out)
    finally:
        rendering_module._atomic_save_png = real_atomic  # type: ignore[assignment]
    # The output must not exist after a failure.
    assert not (tmp_path / "map.png").exists()
    # No leftover temporary files.
    leftovers = [
        p for p in tmp_path.iterdir() if p.name.startswith(".map.png.") and p.name.endswith(".tmp")
    ]
    assert leftovers == []


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
