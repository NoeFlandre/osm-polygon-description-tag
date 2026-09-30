"""Offline Hub API doubles shared by preflight and publication tests."""

from types import SimpleNamespace


class FakeHuggingFaceApi:
    """Implement the small HfApi surface required by offline tests."""

    def whoami(self) -> object:
        return {"name": "fake"}

    def repo_info(self, *_args: object, **_kwargs: object) -> object:
        return SimpleNamespace(sha="abc")

    def auth_check(self, *_args: object, **_kwargs: object) -> None:
        return None


def fake_hf_api(*_args: object, **_kwargs: object) -> FakeHuggingFaceApi:
    """Return a fresh authenticated-looking Hub stub for each HfApi call."""
    return FakeHuggingFaceApi()


class PathInfo:
    """Represent one remote Hub file entry with optional fixture defaults."""

    def __init__(self, *, path: str, size: int = 0, sha: str = "0" * 64) -> None:
        self.path = path
        self.size = size
        self.sha = sha
        self.lfs = None
