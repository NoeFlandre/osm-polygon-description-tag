"""Shared atomic PNG write contract for geography renderers."""

from __future__ import annotations

from pathlib import Path

import pytest

from osm_polygon_description_tag.dataset.geography.atomic import atomic_save_png


class _FakeFigure:
    def savefig(self, path: str, **_: object) -> None:
        Path(path).write_bytes(b"fake-png")


def test_both_geography_renderers_use_the_shared_writer() -> None:
    from osm_polygon_description_tag.dataset.geography.area_rendering import (
        _atomic_save_png as area_atomic_save_png,
    )
    from osm_polygon_description_tag.dataset.geography.rendering import (
        _atomic_save_png as map_atomic_save_png,
    )

    assert area_atomic_save_png is atomic_save_png
    assert map_atomic_save_png is atomic_save_png


def test_atomic_save_png_preserves_identical_output_mtime(tmp_path: Path) -> None:
    output = tmp_path / "image.png"
    figure = _FakeFigure()

    atomic_save_png(figure, output)
    first_mtime = output.stat().st_mtime_ns

    atomic_save_png(figure, output)

    assert output.read_bytes() == b"fake-png"
    assert output.stat().st_mtime_ns == first_mtime
    assert list(tmp_path.glob(".image.png.*.tmp")) == []


def test_atomic_save_png_cleans_temporary_file_when_render_fails(tmp_path: Path) -> None:
    output = tmp_path / "image.png"

    class FailingFigure:
        def savefig(self, _path: str, **_: object) -> None:
            raise RuntimeError("render failed")

    with pytest.raises(RuntimeError, match="render failed"):
        atomic_save_png(FailingFigure(), output)

    assert not output.exists()
    assert list(tmp_path.glob(".image.png.*.tmp")) == []
