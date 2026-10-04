"""Validation of raw source identities supplied alongside manifests."""

import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from osm_polygon_description_tag import cli
from osm_polygon_description_tag.dataset.manifest import source_identity_for
from osm_polygon_description_tag.dataset.storage import StorageError


def _source_manifest(name: str) -> SimpleNamespace:
    return SimpleNamespace(source=SimpleNamespace(name=name))


def test_source_validation_rejects_a_missing_file_with_context(tmp_path: Path) -> None:
    source_root = tmp_path / "raw"
    source_root.mkdir()

    with pytest.raises(StorageError) as error:
        cli._validate_source_file_identity(_source_manifest("missing.osm.pbf"), source_root)

    assert str(error.value) == (
        f"source identity mismatch: missing regular source file {source_root / 'missing.osm.pbf'}"
    )


def test_source_validation_rejects_a_symlink_even_when_its_identity_matches(
    tmp_path: Path,
) -> None:
    source_root = tmp_path / "raw"
    source_root.mkdir()
    target = source_root / "source-bytes.osm.pbf"
    target.write_bytes(b"tiny source fixture")
    source = source_root / "linked.osm.pbf"
    source.symlink_to(target)
    manifest = SimpleNamespace(source=source_identity_for(source))

    with pytest.raises(StorageError) as error:
        cli._validate_source_file_identity(manifest, source_root)

    assert str(error.value) == (
        f"source identity mismatch: missing regular source file {source_root / 'linked.osm.pbf'}"
    )


def test_source_validation_preserves_file_inspection_context(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    source_root = tmp_path / "raw"
    source_root.mkdir()
    source = source_root / "region.osm.pbf"
    source.write_bytes(b"tiny source fixture")
    original_is_symlink = Path.is_symlink

    def fail_for_source(path: Path) -> bool:
        if path == source:
            raise OSError("permission denied")
        return original_is_symlink(path)

    monkeypatch.setattr(Path, "is_symlink", fail_for_source)

    with pytest.raises(StorageError) as error:
        cli._validate_source_file_identity(_source_manifest(source.name), source_root)

    assert str(error.value) == f"cannot inspect source file {source}: permission denied"


def test_source_validation_preserves_file_read_context(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    source_root = tmp_path / "raw"
    source_root.mkdir()
    source = source_root / "region.osm.pbf"
    source.write_bytes(b"tiny source fixture")

    def fail_read(_path: Path) -> None:
        raise OSError("disk error")

    monkeypatch.setattr(cli, "source_identity_for", fail_read)

    with pytest.raises(StorageError) as error:
        cli._validate_source_file_identity(_source_manifest(source.name), source_root)

    assert str(error.value) == f"cannot read source file {source}: disk error"


def test_validate_passes_the_artifact_path_and_manifest_source_to_storage(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    data_dir = tmp_path / "data"
    data_dir.mkdir()
    parquet = data_dir / "region.parquet"
    manifest_path = tmp_path / "manifests" / "region.manifest.json"
    manifest = SimpleNamespace(
        source=SimpleNamespace(name="region.osm.pbf"),
        counts=SimpleNamespace(included_rows=1, emitted_features=1, rejections={}),
    )
    calls: list[tuple[Path, dict[str, str]]] = []

    monkeypatch.setattr(cli, "_validation_source_root", lambda *_args: None)
    monkeypatch.setattr(
        cli,
        "validate_finalized_artifacts",
        lambda *_args, **_kwargs: {"parquets": (parquet,), "manifests": (manifest_path,)},
    )
    monkeypatch.setattr(cli, "read_manifest", lambda _path: manifest)

    def validate(path: Path, **kwargs: str) -> int:
        calls.append((path, kwargs))
        return 1

    monkeypatch.setattr(cli, "validate_geoparquet", validate)

    exit_code = cli.handle_validate(SimpleNamespace(data_root=tmp_path))

    assert exit_code == 0
    assert calls == [(parquet, {"expected_source_pbf": "region.osm.pbf"})]
    assert json.loads(capsys.readouterr().out) == {"files": 1, "rows": 1}


def test_validate_reports_when_manifest_source_name_does_not_map_to_artifact(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    data_dir = tmp_path / "data"
    data_dir.mkdir()
    parquet = data_dir / "region.parquet"
    manifest_path = tmp_path / "manifests" / "region.manifest.json"
    manifest = SimpleNamespace(
        source=SimpleNamespace(name="different.osm.pbf"),
        counts=SimpleNamespace(included_rows=1, emitted_features=1, rejections={}),
    )
    monkeypatch.setattr(cli, "_validation_source_root", lambda *_args: None)
    monkeypatch.setattr(
        cli,
        "validate_finalized_artifacts",
        lambda *_args, **_kwargs: {"parquets": (parquet,), "manifests": (manifest_path,)},
    )
    monkeypatch.setattr(cli, "read_manifest", lambda _path: manifest)
    monkeypatch.setattr(cli, "validate_geoparquet", lambda *_args, **_kwargs: 1)

    with pytest.raises(StorageError) as error:
        cli.handle_validate(SimpleNamespace(data_root=tmp_path))

    assert str(error.value) == (
        "manifest source identity mismatch for region.parquet: "
        "source 'different.osm.pbf' maps to 'different.parquet'"
    )
