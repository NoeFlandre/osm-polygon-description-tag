from __future__ import annotations

from io import StringIO

from osm_polygon_description_tag.runtime.presentation import TerminalPresenter


class TerminalBuffer(StringIO):
    def __init__(self, *, tty: bool) -> None:
        super().__init__()
        self._tty = tty

    def isatty(self) -> bool:
        return self._tty


def test_non_tty_progress_is_silent() -> None:
    stream = TerminalBuffer(tty=False)
    presenter = TerminalPresenter(stderr=stream)

    presenter.observe(
        {
            "event": "build_progress",
            "source": "a.osm.pbf",
            "emitted": 100_000,
        }
    )

    assert stream.getvalue() == ""
    assert presenter.progress_active is False


def test_tty_build_progress_uses_tqdm_and_closes() -> None:
    stream = TerminalBuffer(tty=True)
    presenter = TerminalPresenter(stderr=stream)

    presenter.observe({"event": "build_start", "source": "a.osm.pbf"})
    presenter.observe({"event": "build_progress", "source": "a.osm.pbf", "emitted": 100_000})
    presenter.observe({"event": "build_complete", "source": "a.osm.pbf"})

    assert "a.osm.pbf" in stream.getvalue()
    assert "100000" in stream.getvalue().replace(",", "")
    assert presenter.progress_active is False


def test_error_is_plain_without_tty_and_rich_on_tty() -> None:
    plain = TerminalBuffer(tty=False)
    rich = TerminalBuffer(tty=True)

    TerminalPresenter(stderr=plain).error("boom")
    TerminalPresenter(stderr=rich).error("boom")

    assert plain.getvalue() == "error: boom\n"
    assert "\x1b[" not in plain.getvalue()
    assert "error:" in rich.getvalue()
    assert "boom" in rich.getvalue()
