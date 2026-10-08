"""UTF-8 text and byte conversions shared by every reader and writer.

The codec is named once, here, so every call site reads and writes the same
encoding and no caller restates the codec name. Writers that must replace a
file atomically keep using ``atomic_write_text`` from ``runtime.atomic``.
"""

from pathlib import Path

_UTF8 = "utf-8"


def read_text_utf8(path: Path) -> str:
    """Read ``path`` as UTF-8 text, with the newline translation of ``Path.read_text``."""
    return path.read_text(encoding=_UTF8)


def write_text_utf8(path: Path, text: str) -> None:
    """Write ``text`` to ``path`` as UTF-8, in place, with ``Path.write_text`` newline handling."""
    path.write_text(text, encoding=_UTF8)


def utf8_bytes(text: str) -> bytes:
    """Encode ``text`` as UTF-8 bytes without newline translation."""
    return text.encode(_UTF8)


def decode_utf8(data: bytes, *, errors: str = "strict") -> str:
    """Decode ``data`` as UTF-8 text without newline translation, with ``errors`` handling."""
    return data.decode(_UTF8, errors=errors)  # pragma: no mutate - bytes.decode defaults to UTF-8


__all__ = ["decode_utf8", "read_text_utf8", "utf8_bytes", "write_text_utf8"]
