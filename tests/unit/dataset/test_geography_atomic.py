"""Shared atomic PNG write contract for geography renderers."""

from __future__ import annotations

import os
from pathlib import Path
from unittest.mock import call, patch

import pytest

import osm_polygon_description_tag.dataset.geography.atomic as atomic
from osm_polygon_description_tag.dataset.geography.atomic import atomic_save_png


class _FakeFigure:
    def savefig(self, path: str, **_: object) -> None:
        Path(path).write_bytes(b"fake-png")


class _RecordingFigure:
    def __init__(self) -> None:
        self.calls: list[tuple[str, dict[str, object]]] = []

    def savefig(self, path: str, **kwargs: object) -> None:
        self.calls.append((path, kwargs))
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

    with patch.object(atomic.os, "replace", wraps=atomic.os.replace) as replace_mock:
        atomic_save_png(figure, output)
        first_mtime = output.stat().st_mtime_ns

        replace_mock.reset_mock()
        atomic_save_png(figure, output)
        replace_mock.assert_not_called()

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


def test_atomic_save_png_uses_nested_parent_and_exact_writer_options(tmp_path: Path) -> None:
    output = tmp_path / "nested" / "deeper" / "image.png"
    figure = _RecordingFigure()

    with (
        patch.object(atomic.tempfile, "mkstemp", wraps=atomic.tempfile.mkstemp) as mkstemp,
        patch.object(atomic.os, "open", wraps=atomic.os.open) as open_mock,
        patch.object(atomic, "open", wraps=open, create=True) as open_mock_builtin,
    ):
        atomic_save_png(figure, output)

    assert output.read_bytes() == b"fake-png"
    assert mkstemp.call_args.kwargs == {
        "prefix": ".image.png.",
        "suffix": ".tmp",
        "dir": str(output.parent),
    }
    assert figure.calls == [
        (
            figure.calls[0][0],
            {
                "format": "png",
                "facecolor": "white",
                "metadata": {"Software": "osm-polygon-description-tag"},
            },
        )
    ]
    assert open_mock_builtin.call_args_list == [call(Path(figure.calls[0][0]), "rb")]
    assert open_mock.call_args == call(str(output.parent), os.O_RDONLY)


def test_atomic_save_png_keeps_original_error_when_temp_file_was_removed(tmp_path: Path) -> None:
    output = tmp_path / "image.png"

    class RemovesTempThenFails:
        def savefig(self, path: str, **_: object) -> None:
            Path(path).unlink()
            raise RuntimeError("render failed")

    with pytest.raises(RuntimeError, match="render failed"):
        atomic_save_png(RemovesTempThenFails(), output)

    assert not output.exists()
