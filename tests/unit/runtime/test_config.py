from pathlib import Path

import pytest

from osm_polygon_description_tag.runtime.config import (
    DATA_ROOT_ENV,
    SOURCE_ROOT_ENV,
    MissingPathError,
    Paths,
    UnsafePathError,
    resolve_data_root,
)


def test_cli_options_beat_the_environment(tmp_path: Path) -> None:
    env = {SOURCE_ROOT_ENV: str(tmp_path / "env-raw"), DATA_ROOT_ENV: str(tmp_path / "env-out")}

    paths = Paths.resolve(tmp_path / "raw", tmp_path / "out", env)

    assert paths == Paths(tmp_path / "raw", tmp_path / "out")


def test_the_environment_fills_missing_options(tmp_path: Path) -> None:
    env = {SOURCE_ROOT_ENV: f"  {tmp_path / 'raw'}  ", DATA_ROOT_ENV: str(tmp_path / "out")}

    assert Paths.resolve(None, None, env) == Paths(tmp_path / "raw", tmp_path / "out")
    assert Paths.resolve(tmp_path / "cli", None, env).data_root == tmp_path / "out"


@pytest.mark.parametrize(
    ("env", "message"),
    [
        ({}, "no --source-root given and OSM_POLYGON_SOURCE_ROOT is not set"),
        (
            {SOURCE_ROOT_ENV: "/raw", DATA_ROOT_ENV: "  "},
            "no --data-root given and OSM_POLYGON_DATA_ROOT",
        ),
    ],
)
def test_a_missing_root_names_both_the_flag_and_the_variable(
    env: dict[str, str], message: str
) -> None:
    with pytest.raises(MissingPathError, match=message):
        Paths.resolve(None, None, env)


def test_resolve_reads_os_environ_by_default(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv(SOURCE_ROOT_ENV, str(tmp_path / "raw"))
    monkeypatch.setenv(DATA_ROOT_ENV, str(tmp_path / "out"))

    assert Paths.resolve(None, None) == Paths(tmp_path / "raw", tmp_path / "out")


def test_resolve_still_enforces_containment(tmp_path: Path) -> None:
    with pytest.raises(UnsafePathError, match="inside immutable source"):
        Paths.resolve(tmp_path / "raw", tmp_path / "raw" / "out", {})


def test_output_cannot_be_inside_source(tmp_path: Path) -> None:
    source = tmp_path / "raw"
    source.mkdir()
    with pytest.raises(UnsafePathError, match="inside immutable source"):
        Paths(source_root=source, data_root=source / "output").validate()


def test_source_inside_data_root_is_also_rejected(tmp_path: Path) -> None:
    data = tmp_path / "data"
    data.mkdir()
    with pytest.raises(UnsafePathError, match="inside data root"):
        Paths(source_root=data / "raw", data_root=data).validate()


def test_disjoint_roots_validate(tmp_path: Path) -> None:
    source = tmp_path / "raw"
    data = tmp_path / "data"
    source.mkdir()
    data.mkdir()
    paths = Paths(source_root=source, data_root=data)
    assert paths.validate() is paths


def test_resolve_data_root_prefers_the_option(tmp_path: Path) -> None:
    assert resolve_data_root(tmp_path, {DATA_ROOT_ENV: "/elsewhere"}) == tmp_path


def test_resolve_data_root_needs_no_source_root(tmp_path: Path) -> None:
    assert resolve_data_root(None, {DATA_ROOT_ENV: str(tmp_path)}) == tmp_path


def test_resolve_data_root_reads_os_environ_by_default(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setenv(DATA_ROOT_ENV, str(tmp_path))
    assert resolve_data_root(None) == tmp_path


def test_resolve_data_root_names_the_flag_when_missing() -> None:
    with pytest.raises(MissingPathError) as error:
        resolve_data_root(None, {})
    assert str(error.value) == (
        "no --data-root given and OSM_POLYGON_DATA_ROOT is not set; pass one of them"
    )
