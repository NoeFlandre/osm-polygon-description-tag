from pathlib import Path

import pytest

from osm_polygon_description_tag.runtime.text_io import (
    decode_utf8,
    read_text_utf8,
    utf8_bytes,
    write_text_utf8,
)


def test_utf8_bytes_encodes_non_ascii_text_as_utf8() -> None:
    assert utf8_bytes("é☃") == b"\xc3\xa9\xe2\x98\x83"


def test_utf8_bytes_keeps_newlines_untouched() -> None:
    assert utf8_bytes("a\r\nb\n") == b"a\r\nb\n"


def test_decode_utf8_keeps_carriage_returns_untranslated() -> None:
    assert decode_utf8(b"\xc3\xa9\r\nx") == "é\r\nx"


def test_read_text_utf8_matches_path_read_text_with_utf8(tmp_path: Path) -> None:
    target = tmp_path / "in.txt"
    target.write_bytes("café\r\nligne\n".encode())

    assert read_text_utf8(target) == target.read_text(encoding="utf-8")
    assert read_text_utf8(target) == "café\nligne\n"


def test_write_text_utf8_writes_utf8_bytes_in_place(tmp_path: Path) -> None:
    target = tmp_path / "out.txt"

    write_text_utf8(target, "café\n")

    assert target.read_bytes() == "café\n".encode()
    assert read_text_utf8(target) == "café\n"


def test_read_text_utf8_uses_explicit_utf8_encoding(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    target = tmp_path / "in.txt"
    target.write_bytes(b"x")
    encodings: list[object] = []
    real_read_text = Path.read_text

    def spy(self: Path, *args: object, **kwargs: object) -> str:
        encodings.append(kwargs.get("encoding"))
        return real_read_text(self, *args, **kwargs)  # type: ignore[arg-type]

    monkeypatch.setattr(Path, "read_text", spy)

    assert read_text_utf8(target) == "x"
    assert encodings == ["utf-8"]


def test_write_text_utf8_uses_explicit_utf8_encoding(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    encodings: list[object] = []
    real_write_text = Path.write_text

    def spy(self: Path, data: str, *args: object, **kwargs: object) -> int:
        encodings.append(kwargs.get("encoding"))
        return real_write_text(self, data, *args, **kwargs)  # type: ignore[arg-type]

    monkeypatch.setattr(Path, "write_text", spy)

    write_text_utf8(tmp_path / "out.txt", "x")

    assert encodings == ["utf-8"]


def test_decode_utf8_is_strict_by_default() -> None:
    with pytest.raises(UnicodeDecodeError):
        decode_utf8(b"\xff")


def test_decode_utf8_forwards_an_explicit_error_handler() -> None:
    assert decode_utf8(b"a\xffb", errors="replace") == "a�b"
