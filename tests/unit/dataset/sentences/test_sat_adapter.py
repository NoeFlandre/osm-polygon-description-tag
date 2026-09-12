"""Exact construction and call contracts of the pinned SaT-3l-sm adapter.

No test loads the real model: the runtime is faked, exactly as the GlotLID
adapter's tests fake fastText. What is asserted is the pinning --- the right
repository, revision and digest --- and that the adapter refuses anything it
cannot vouch for rather than segmenting it anyway.
"""

from __future__ import annotations

import hashlib
from pathlib import Path
from types import ModuleType, SimpleNamespace

import pytest

from osm_polygon_description_tag.dataset.sentences import sat as sat_module
from osm_polygon_description_tag.dataset.sentences.sat import (
    SAT_MODEL_REPOSITORY,
    SAT_MODEL_REVISION,
    SAT_RUNTIME_LIBRARY_NAME,
    SAT_RUNTIME_LIBRARY_VERSION,
    SentenceSplitterError,
    build_sat_splitter,
)
from tests.helpers.messages import exactly


class _FakeModel:
    def __init__(self) -> None:
        self.calls: list[object] = []

    def split(self, text: str, **kwargs: object) -> list[str]:
        self.calls.append((text, kwargs))
        return ["One. ", "Two."]


def _install_fake_runtime(
    monkeypatch: pytest.MonkeyPatch, model: object, *, version: str | None = None
) -> list[object]:
    loads: list[object] = []
    module = ModuleType("wtpsplit")

    def sat(path: str, *, tokenizer_name_or_path: str) -> object:
        loads.append((path, tokenizer_name_or_path))
        return model

    module.SaT = sat  # type: ignore[attr-defined]

    def import_module(name: str) -> ModuleType:
        IMPORTED.append(name)
        return module

    def distribution_version(name: str) -> str:
        QUERIED.append(name)
        return version or SAT_RUNTIME_LIBRARY_VERSION

    IMPORTED.clear()
    QUERIED.clear()
    monkeypatch.setattr(sat_module.importlib, "import_module", import_module)
    monkeypatch.setattr(sat_module.metadata, "version", distribution_version)
    return loads


IMPORTED: list[str] = []
QUERIED: list[str] = []


def _pinned_model(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Stage a directory shaped exactly like the published SaT repository."""
    model_dir = tmp_path / "sat-3l-sm"
    model_dir.mkdir()
    (model_dir / "model.safetensors").write_bytes(b"weights")
    (model_dir / "config.json").write_text("{}", encoding="utf-8")
    monkeypatch.setattr(sat_module, "SAT_MODEL_SHA256", hashlib.sha256(b"weights").hexdigest())
    return model_dir


def test_the_pinned_artifact_is_the_published_sat_3l_sm_revision() -> None:
    """These three values are the whole provenance claim for the splitter."""
    assert SAT_MODEL_REPOSITORY == "segment-any-text/sat-3l-sm"
    assert SAT_MODEL_REVISION == "137da054051ad9f1eac42025f758db4ac9f22535"
    assert SAT_RUNTIME_LIBRARY_NAME == "wtpsplit"


def test_the_model_and_its_tokenizer_come_from_the_verified_directory(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The tokenizer is named explicitly so nothing reaches the Hub on a node."""
    model = _FakeModel()
    loads = _install_fake_runtime(monkeypatch, model)
    path = _pinned_model(tmp_path, monkeypatch)

    build_sat_splitter(model_dir=path)

    assert loads == [(str(path), str(path))]


def test_a_model_whose_digest_is_not_the_pinned_artifact_is_refused_exactly(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    model = _FakeModel()
    loads = _install_fake_runtime(monkeypatch, model)
    path = tmp_path / "sat-3l-sm"
    path.mkdir()
    (path / "model.safetensors").write_bytes(b"wrong bytes")
    (path / "config.json").write_text("{}", encoding="utf-8")

    with pytest.raises(SentenceSplitterError) as caught:
        build_sat_splitter(model_dir=path)

    assert str(caught.value) == "SaT model hash does not match the pinned sat-3l-sm artifact"
    assert loads == []


def test_a_runtime_of_the_wrong_version_is_refused_with_its_exact_reason(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _install_fake_runtime(monkeypatch, _FakeModel(), version="9.9.9")
    path = _pinned_model(tmp_path, monkeypatch)

    with pytest.raises(SentenceSplitterError) as caught:
        build_sat_splitter(model_dir=path)

    assert str(caught.value) == (
        f"installed wtpsplit version '9.9.9'; required {SAT_RUNTIME_LIBRARY_VERSION!r}"
    )


def test_splitting_returns_the_runtimes_segments_verbatim(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Segments are published as the model produced them, whitespace included."""
    model = _FakeModel()
    _install_fake_runtime(monkeypatch, model)
    splitter = build_sat_splitter(model_dir=_pinned_model(tmp_path, monkeypatch))

    assert splitter.split("One. Two.", language="en") == ("One. ", "Two.")
    assert model.calls == [("One. Two.", {})]


def test_a_language_outside_the_supported_set_is_refused_by_the_adapter_too(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The gate decides; the adapter refuses to be used against that decision."""
    model = _FakeModel()
    _install_fake_runtime(monkeypatch, model)
    splitter = build_sat_splitter(model_dir=_pinned_model(tmp_path, monkeypatch))

    with pytest.raises(
        SentenceSplitterError, match=exactly("SaT was not trained on language 'hr'")
    ):
        splitter.split("Jedan. Dva.", language="hr")

    assert model.calls == []


def test_a_runtime_that_cannot_be_imported_fails_closed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(
        sat_module.importlib,
        "import_module",
        lambda _name: (_ for _ in ()).throw(ImportError("missing")),
    )
    monkeypatch.setattr(sat_module.metadata, "version", lambda _name: SAT_RUNTIME_LIBRARY_VERSION)
    path = _pinned_model(tmp_path, monkeypatch)

    with pytest.raises(
        SentenceSplitterError, match=exactly("could not import the pinned wtpsplit runtime")
    ):
        build_sat_splitter(model_dir=path)


def test_a_runtime_without_a_sat_entry_point_is_refused(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    module = ModuleType("wtpsplit")
    monkeypatch.setattr(sat_module.importlib, "import_module", lambda _name: module)
    monkeypatch.setattr(sat_module.metadata, "version", lambda _name: SAT_RUNTIME_LIBRARY_VERSION)
    path = _pinned_model(tmp_path, monkeypatch)

    with pytest.raises(SentenceSplitterError, match=exactly("wtpsplit runtime lacks SaT")):
        build_sat_splitter(model_dir=path)


def test_a_model_directory_that_is_not_a_directory_is_refused_by_its_path(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _install_fake_runtime(monkeypatch, _FakeModel())
    missing = tmp_path / "absent"

    with pytest.raises(
        SentenceSplitterError,
        match=exactly(f"SaT model directory must be a directory: {missing}"),
    ):
        build_sat_splitter(model_dir=missing)


@pytest.mark.parametrize("missing", ["model.safetensors", "config.json"])
def test_a_model_directory_missing_a_required_file_names_that_file(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, missing: str
) -> None:
    """``wtpsplit`` loads a directory, so both files have to be staged."""
    _install_fake_runtime(monkeypatch, _FakeModel())
    model_dir = _pinned_model(tmp_path, monkeypatch)
    (model_dir / missing).unlink()

    with pytest.raises(
        SentenceSplitterError,
        match=exactly(f"SaT model directory is missing {missing}: {model_dir}"),
    ):
        build_sat_splitter(model_dir=model_dir)


def test_a_non_string_segment_from_the_runtime_is_refused(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A published sentence must be text; anything else fails closed."""
    model = SimpleNamespace(split=lambda text, **kwargs: ["fine", 7])
    _install_fake_runtime(monkeypatch, model)
    splitter = build_sat_splitter(model_dir=_pinned_model(tmp_path, monkeypatch))

    with pytest.raises(SentenceSplitterError, match=exactly("SaT segments must be strings")):
        splitter.split("whatever", language="en")


@pytest.mark.parametrize("raw", ["one sentence", 7, None])
def test_a_runtime_that_returns_something_other_than_a_sequence_is_refused(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, raw: object
) -> None:
    """A bare string would silently publish one sentence per character."""
    model = SimpleNamespace(split=lambda text, **kwargs: raw)
    _install_fake_runtime(monkeypatch, model)
    splitter = build_sat_splitter(model_dir=_pinned_model(tmp_path, monkeypatch))

    with pytest.raises(SentenceSplitterError, match=exactly("SaT segments must be a sequence")):
        splitter.split("whatever", language="en")


def test_a_runtime_that_cannot_load_the_model_fails_closed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    module = ModuleType("wtpsplit")

    def refuse(_path: str, **_kwargs: object) -> object:
        raise OSError("corrupt weights")

    module.SaT = refuse  # type: ignore[attr-defined]
    monkeypatch.setattr(sat_module.importlib, "import_module", lambda _name: module)
    monkeypatch.setattr(sat_module.metadata, "version", lambda _name: SAT_RUNTIME_LIBRARY_VERSION)

    with pytest.raises(
        SentenceSplitterError, match=exactly("could not load the pinned sat-3l-sm model")
    ):
        build_sat_splitter(model_dir=_pinned_model(tmp_path, monkeypatch))


def test_an_uninstalled_runtime_names_the_extra_that_provides_it(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def missing(name: str) -> str:
        raise sat_module.metadata.PackageNotFoundError(name)

    monkeypatch.setattr(sat_module.metadata, "version", missing)

    with pytest.raises(
        SentenceSplitterError,
        match=exactly("wtpsplit is not installed; install the language extra"),
    ):
        build_sat_splitter(model_dir=tmp_path / "sat-3l-sm")


def test_an_unhashable_model_path_is_reported_by_its_own_path(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A weights file that cannot be read must name itself, not fail obscurely."""
    _install_fake_runtime(monkeypatch, _FakeModel())
    model_dir = _pinned_model(tmp_path, monkeypatch)
    weights = model_dir / "model.safetensors"
    weights.chmod(0o000)

    try:
        with pytest.raises(SentenceSplitterError) as caught:
            build_sat_splitter(model_dir=model_dir)
    finally:
        weights.chmod(0o600)

    assert str(caught.value) == f"could not hash the SaT model: {weights}"


def test_the_pinned_runtime_is_the_one_imported_and_version_checked(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Importing or version-checking some other distribution would prove nothing."""
    _install_fake_runtime(monkeypatch, _FakeModel())

    build_sat_splitter(model_dir=_pinned_model(tmp_path, monkeypatch))

    assert IMPORTED == [SAT_RUNTIME_LIBRARY_NAME]
    assert QUERIED == [SAT_RUNTIME_LIBRARY_NAME]
