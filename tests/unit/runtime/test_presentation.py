from __future__ import annotations

from io import StringIO
from unittest.mock import Mock, call, patch

import osm_polygon_description_tag.runtime.presentation as presentation
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


def test_presenter_uses_stable_console_options_and_safe_isatty_fallback() -> None:
    class NoIsatty:
        pass

    stderr = NoIsatty()
    with patch.object(presentation, "Console") as console:
        presenter = TerminalPresenter(stderr=stderr)  # type: ignore[arg-type]

    assert presenter._interactive is False
    console.assert_called_once_with(file=stderr, force_terminal=False, color_system="auto")


def test_presenter_starts_progress_with_exact_tqdm_contract() -> None:
    stream = TerminalBuffer(tty=True)
    presenter = TerminalPresenter(stderr=stream)
    progress = Mock(n=0)

    with patch.object(presentation, "tqdm", return_value=progress) as tqdm_mock:
        presenter._start_progress({})

    tqdm_mock.assert_called_once_with(
        total=None,
        desc="",
        unit="features",
        file=stream,
        disable=False,
    )
    assert presenter.progress_active is True


def test_presenter_updates_progress_defensively_and_monotonically() -> None:
    stream = TerminalBuffer(tty=True)
    presenter = TerminalPresenter(stderr=stream)
    progress = Mock(n=0)
    presenter._progress = progress

    presenter._update_progress({})
    presenter._update_progress({"emitted": "not-an-int"})
    progress.n = 5
    presenter._update_progress({"emitted": 8})

    assert progress.update.call_args_list == [call(0), call(0), call(3)]


def test_interrupted_event_closes_active_progress() -> None:
    stream = TerminalBuffer(tty=True)
    presenter = TerminalPresenter(stderr=stream)
    progress = Mock(n=0)

    with patch.object(presentation, "tqdm", return_value=progress):
        presenter.observe({"event": "build_start", "source": "a.osm.pbf"})
        presenter.observe({"event": "interrupted", "source": "a.osm.pbf"})

    progress.close.assert_called_once_with()
    assert presenter.progress_active is False
