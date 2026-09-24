"""The Hub verifier reads small text artifacts from an exact revision."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from osm_polygon_description_tag.publication import verification


class _Api:
    def __init__(self, local: Path | None = None, error: Exception | None = None) -> None:
        self.local = local
        self.error = error
        self.calls: list[tuple[tuple[Any, ...], dict[str, Any]]] = []

    def hf_hub_download(self, *args: Any, **kwargs: Any) -> str:
        self.calls.append((args, kwargs))
        if self.error is not None:
            raise self.error
        return str(self.local)


def test_read_file_downloads_the_exact_revision_and_decodes_utf8(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    local = tmp_path / "README.md"
    local.write_text("café", encoding="utf-8")
    api = _Api(local)
    monkeypatch.setattr(verification, "_authenticated_api", lambda: api)

    assert verification._read_file("owner/repo", "README.md", "abc", None) == "café"
    assert api.calls == [(("owner/repo", "README.md"), {"revision": "abc", "repo_type": "dataset"})]


def test_read_file_forwards_an_explicit_cache_dir(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    local = tmp_path / "x.txt"
    local.write_text("x", encoding="utf-8")
    api = _Api(local)
    monkeypatch.setattr(verification, "_authenticated_api", lambda: api)

    verification._read_file("owner/repo", "x.txt", "abc", tmp_path)
    assert api.calls[0][1]["cache_dir"] == tmp_path


def test_read_file_wraps_download_failures(monkeypatch: pytest.MonkeyPatch) -> None:
    cause = RuntimeError("boom")
    monkeypatch.setattr(verification, "_authenticated_api", lambda: _Api(error=cause))

    with pytest.raises(verification.HubVerificationError) as raised:
        verification._read_file("owner/repo", "README.md", "abc", None)
    assert str(raised.value) == "could not read README.md from owner/repo@abc: boom"
    assert raised.value.__cause__ is cause
