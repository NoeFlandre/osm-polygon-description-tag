"""Public CLI stream and exit-code contracts independent of parser ownership."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

import osm_polygon_description_tag.cli as cli


def _inspect_args(source_root: Path, data_root: Path) -> list[str]:
    return [
        "inspect",
        "--source-root",
        str(source_root),
        "--data-root",
        str(data_root),
    ]


def test_success_stdout_is_exactly_one_json_document(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    source_root = tmp_path / "raw"
    source_root.mkdir()
    (source_root / "region.osm.pbf").write_bytes(b"synthetic")
    data_root = tmp_path / "generated"

    exit_code = cli.run(_inspect_args(source_root, data_root))
    captured = capsys.readouterr()
    decoder = json.JSONDecoder()
    payload, end = decoder.raw_decode(captured.out)

    assert exit_code == 0
    assert payload["source_count"] == 1
    assert captured.out[end:].strip() == ""
    assert captured.err == ""
    assert "\x1b[" not in captured.out


def test_domain_error_is_exact_plain_stderr(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    def fail_discovery(_root: Path) -> object:
        raise ValueError("boom")

    monkeypatch.setattr(cli, "discover_sources", fail_discovery)

    exit_code = cli.run(_inspect_args(tmp_path / "raw", tmp_path / "generated"))
    captured = capsys.readouterr()

    assert exit_code == 1
    assert captured.out == ""
    assert captured.err == "error: boom\n"
    assert "\x1b[" not in captured.err


def test_keyboard_interrupt_returns_130_without_output(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    def interrupt_discovery(_root: Path) -> object:
        raise KeyboardInterrupt

    monkeypatch.setattr(cli, "discover_sources", interrupt_discovery)

    exit_code = cli.run(_inspect_args(tmp_path / "raw", tmp_path / "generated"))
    captured = capsys.readouterr()

    assert exit_code == 130
    assert captured.out == ""
    assert captured.err == ""


def test_noninteractive_success_emits_no_progress_or_ansi(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    source_root = tmp_path / "raw"
    source_root.mkdir()

    exit_code = cli.run(_inspect_args(source_root, tmp_path / "generated"))
    captured = capsys.readouterr()

    assert exit_code == 0
    assert captured.err == ""
    assert "\x1b[" not in captured.out
    assert "\r" not in captured.out
