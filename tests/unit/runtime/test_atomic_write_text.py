from pathlib import Path

import pytest

from osm_polygon_description_tag.runtime.atomic import atomic_write_text


def test_atomic_write_text_writes_utf8_with_explicit_encoding(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    encodings: list[object] = []
    newlines: list[object] = []
    real_write_text = Path.write_text

    def spy(self: Path, data: str, *args: object, **kwargs: object) -> int:
        encodings.append(kwargs.get("encoding"))
        newlines.append(kwargs.get("newline"))
        return real_write_text(self, data, *args, **kwargs)  # type: ignore[arg-type]

    monkeypatch.setattr(Path, "write_text", spy)
    target = tmp_path / "nested" / "out.txt"

    atomic_write_text(target, "café\n")

    assert target.read_bytes() == "café\n".encode()
    assert encodings == ["utf-8"]
    assert newlines == [""]
    assert list(target.parent.glob(".*")) == []


@pytest.mark.parametrize("text", ["a\nb\n", "a\r\nb\r\n", "a\rb", "mixed\r\n\n"])
def test_atomic_write_text_preserves_newlines_byte_exact(tmp_path: Path, text: str) -> None:
    target = tmp_path / "out.txt"

    atomic_write_text(target, text)

    assert target.read_bytes() == text.encode("utf-8")
