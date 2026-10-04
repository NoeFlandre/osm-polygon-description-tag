import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from osm_polygon_description_tag import cli
from osm_polygon_description_tag.dataset.manifest import (
    _manifest_path_for,
    output_identity_for,
    source_identity_for,
    write_manifest,
)
from osm_polygon_description_tag.dataset.storage import StorageError


def _write_artifact_pair(
    tmp_path: Path, data_root: Path, manifest_factory, name: str = "a"
) -> Path:
    data_dir = data_root / "data"
    manifests_dir = data_root / "manifests"
    data_dir.mkdir(parents=True, exist_ok=True)
    manifests_dir.mkdir(parents=True, exist_ok=True)
    source = tmp_path / "sources" / f"{name}.osm.pbf"
    source.parent.mkdir(parents=True, exist_ok=True)
    source.write_bytes(b"tiny source fixture")
    parquet = data_dir / f"{name}.parquet"
    parquet.write_bytes(b"tiny parquet fixture")
    write_manifest(
        manifest_factory(
            source=source_identity_for(source),
            output=output_identity_for(parquet),
        ),
        _manifest_path_for(parquet.name, data_root),
    )
    return parquet


def test_validate_rejects_empty_existing_artifact_directories(tmp_path: Path) -> None:
    (tmp_path / "data").mkdir()
    (tmp_path / "manifests").mkdir()

    with pytest.raises(StorageError, match="no finalized data artifacts"):
        cli.handle_validate(SimpleNamespace(data_root=tmp_path))


@pytest.mark.parametrize("unpaired", ["parquet", "manifest"])
def test_validate_rejects_unpaired_artifacts(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, unpaired: str, manifest_factory
) -> None:
    monkeypatch.setattr(cli, "validate_geoparquet", lambda _path: 1)
    data_dir = tmp_path / "data"
    manifests_dir = tmp_path / "manifests"
    data_dir.mkdir()
    manifests_dir.mkdir()
    if unpaired == "parquet":
        (data_dir / "a.parquet").write_bytes(b"tiny parquet fixture")
    else:
        _write_artifact_pair(tmp_path, tmp_path, manifest_factory)
        (data_dir / "a.parquet").unlink()

    with pytest.raises(StorageError, match="artifact/manifest mismatch"):
        cli.handle_validate(SimpleNamespace(data_root=tmp_path))


def test_validate_rejects_a_corrupt_manifest(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setattr(cli, "validate_geoparquet", lambda _path: 1)
    data_dir = tmp_path / "data"
    manifests_dir = tmp_path / "manifests"
    data_dir.mkdir()
    manifests_dir.mkdir()
    (data_dir / "a.parquet").write_bytes(b"tiny parquet fixture")
    (manifests_dir / "a.manifest.json").write_text("{", encoding="utf-8")

    with pytest.raises(StorageError, match="corrupt manifest JSON"):
        cli.handle_validate(SimpleNamespace(data_root=tmp_path))


def test_validate_reports_structurally_invalid_manifest_without_traceback(
    tmp_path: Path, manifest_factory, capsys: pytest.CaptureFixture[str]
) -> None:
    _write_artifact_pair(tmp_path, tmp_path, manifest_factory)
    manifest_path = tmp_path / "manifests" / "a.manifest.json"
    payload = json.loads(manifest_path.read_text(encoding="utf-8"))
    payload.pop("source")
    manifest_path.write_text(json.dumps(payload), encoding="utf-8")

    exit_code = cli.run(["validate", "--data-root", str(tmp_path)])

    assert exit_code == cli.EXIT_VALIDATION
    error = capsys.readouterr().err
    assert "invalid manifest" in error
    assert "Traceback" not in error


@pytest.mark.parametrize(
    "manifest_bytes",
    [
        b"\xff",
        b"[" * 10_000 + b"0" + b"]" * 10_000,
        b'{"value":' + b"9" * 5_000 + b"}",
    ],
    ids=["invalid-utf8", "deeply-nested-json", "integer-digit-limit"],
)
def test_validate_reports_manifest_parse_errors_without_traceback(
    tmp_path: Path,
    manifest_factory,
    capsys: pytest.CaptureFixture[str],
    manifest_bytes: bytes,
) -> None:
    _write_artifact_pair(tmp_path, tmp_path, manifest_factory)
    (tmp_path / "manifests" / "a.manifest.json").write_bytes(manifest_bytes)

    exit_code = cli.run(["validate", "--data-root", str(tmp_path)])

    assert exit_code == cli.EXIT_VALIDATION
    error = capsys.readouterr().err
    assert "invalid manifest" in error
    assert "Traceback" not in error


def test_validate_rejects_a_parquet_that_no_longer_matches_its_manifest(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, manifest_factory
) -> None:
    monkeypatch.setattr(cli, "validate_geoparquet", lambda _path: 1)
    parquet = _write_artifact_pair(tmp_path, tmp_path, manifest_factory)
    parquet.write_bytes(b"changed bytes at the same path")

    with pytest.raises(StorageError, match="stale output identity for a.parquet"):
        cli.handle_validate(SimpleNamespace(data_root=tmp_path))


def test_validate_reports_unreadable_parquet_as_validation_error(
    tmp_path: Path, manifest_factory, capsys: pytest.CaptureFixture[str]
) -> None:
    parquet = _write_artifact_pair(tmp_path, tmp_path, manifest_factory)
    parquet.unlink()
    parquet.mkdir()

    exit_code = cli.run(["validate", "--data-root", str(tmp_path)])

    assert exit_code == cli.EXIT_VALIDATION
    error = capsys.readouterr().err
    assert "cannot read finalized artifact" in error
    assert "Traceback" not in error


def test_validate_accepts_a_partial_set_with_matching_manifests(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, manifest_factory, capsys
) -> None:
    parquet = _write_artifact_pair(tmp_path, tmp_path, manifest_factory, "one-of-many")
    monkeypatch.setattr(cli, "validate_geoparquet", lambda path: 2 if path == parquet else 0)

    assert cli.handle_validate(SimpleNamespace(data_root=tmp_path)) == 0

    assert json.loads(capsys.readouterr().out) == {"files": 1, "rows": 2}
