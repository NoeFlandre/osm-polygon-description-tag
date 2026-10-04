import json
import os
import subprocess
import sys
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
from osm_polygon_description_tag.dataset.storage import StorageError, write_geoparquet


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


def _write_real_artifact_pair(
    data_root: Path,
    source_root: Path,
    manifest_factory,
    record: dict[str, object],
) -> tuple[Path, Path, Path]:
    data_dir = data_root / "data"
    manifests_dir = data_root / "manifests"
    data_dir.mkdir(parents=True, exist_ok=True)
    manifests_dir.mkdir(parents=True, exist_ok=True)
    source_root.mkdir(parents=True, exist_ok=True)
    source = source_root / "a.osm.pbf"
    source.write_bytes(b"tiny source fixture")
    parquet = data_dir / "a.parquet"
    write_geoparquet([dict(record, source_pbf=source.name)], parquet)
    manifest_path = _manifest_path_for(parquet.name, data_root)
    write_manifest(
        manifest_factory(
            source=source_identity_for(source),
            output=output_identity_for(parquet),
        ),
        manifest_path,
    )
    return source, parquet, manifest_path


def test_validate_rejects_empty_existing_artifact_directories(tmp_path: Path) -> None:
    (tmp_path / "data").mkdir()
    (tmp_path / "manifests").mkdir()

    with pytest.raises(StorageError, match="no finalized data artifacts") as exc_info:
        cli.handle_validate(SimpleNamespace(data_root=tmp_path))

    assert str(tmp_path / "data") in str(exc_info.value)


@pytest.mark.parametrize("unpaired", ["parquet", "manifest"])
def test_validate_rejects_unpaired_artifacts(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, unpaired: str, manifest_factory
) -> None:
    monkeypatch.setattr(cli, "validate_geoparquet", lambda _path, **_kwargs: 1)
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
    monkeypatch.setattr(cli, "validate_geoparquet", lambda _path, **_kwargs: 1)
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
    monkeypatch.setattr(cli, "validate_geoparquet", lambda _path, **_kwargs: 1)
    parquet = _write_artifact_pair(tmp_path, tmp_path, manifest_factory)
    parquet.write_bytes(b"changed bytes at the same path")

    with pytest.raises(StorageError, match="stale output identity for a.parquet"):
        cli.handle_validate(SimpleNamespace(data_root=tmp_path))


def test_validate_rejects_a_manifest_source_name_that_disagrees_with_parquet(
    tmp_path: Path,
    manifest_factory,
    valid_records: list[dict[str, object]],
    capsys: pytest.CaptureFixture[str],
) -> None:
    _, _, manifest_path = _write_real_artifact_pair(
        tmp_path / "generated",
        tmp_path / "raw",
        manifest_factory,
        valid_records[0],
    )
    payload = json.loads(manifest_path.read_text(encoding="utf-8"))
    payload["source"]["name"] = "different.osm.pbf"
    manifest_path.write_text(json.dumps(payload), encoding="utf-8")

    exit_code = cli.run(["validate", "--data-root", str(tmp_path / "generated")])

    assert exit_code == cli.EXIT_VALIDATION
    error = capsys.readouterr().err
    assert "source identity mismatch" in error
    assert "different.osm.pbf" in error
    assert "a.osm.pbf" in error
    assert "Traceback" not in error


def test_validate_rejects_a_manifest_source_name_without_pbf_suffix(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    manifest_factory,
    capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.setattr(cli, "validate_geoparquet", lambda _path, **_kwargs: 1)
    _write_artifact_pair(tmp_path, tmp_path, manifest_factory, name="region")
    manifest_path = tmp_path / "manifests" / "region.manifest.json"
    payload = json.loads(manifest_path.read_text(encoding="utf-8"))
    payload["source"]["name"] = "region"
    manifest_path.write_text(json.dumps(payload), encoding="utf-8")

    exit_code = cli.run(["validate", "--data-root", str(tmp_path)])

    assert exit_code == cli.EXIT_VALIDATION
    error = capsys.readouterr().err
    assert "invalid manifest source.name" in error
    assert ".osm.pbf" in error
    assert "Traceback" not in error


@pytest.mark.parametrize("field", ["size_bytes", "mtime_ns", "sha256"])
def test_validate_checks_full_source_identity_when_source_root_is_supplied(
    tmp_path: Path,
    manifest_factory,
    valid_records: list[dict[str, object]],
    capsys: pytest.CaptureFixture[str],
    field: str,
) -> None:
    source_root = tmp_path / "raw"
    _, _, manifest_path = _write_real_artifact_pair(
        tmp_path / "generated",
        source_root,
        manifest_factory,
        valid_records[0],
    )
    payload = json.loads(manifest_path.read_text(encoding="utf-8"))
    if field == "sha256":
        payload["source"][field] = "0" * 64
    else:
        payload["source"][field] += 1
    manifest_path.write_text(json.dumps(payload), encoding="utf-8")

    exit_code = cli.run(
        [
            "validate",
            "--source-root",
            str(source_root),
            "--data-root",
            str(tmp_path / "generated"),
        ]
    )

    assert exit_code == cli.EXIT_VALIDATION
    error = capsys.readouterr().err
    assert "source identity mismatch" in error
    assert "a.osm.pbf" in error
    assert "Traceback" not in error


def test_validate_accepts_a_matching_source_identity_with_source_root(
    tmp_path: Path,
    manifest_factory,
    valid_records: list[dict[str, object]],
    capsys: pytest.CaptureFixture[str],
) -> None:
    source_root = tmp_path / "raw"
    _write_real_artifact_pair(
        tmp_path / "generated",
        source_root,
        manifest_factory,
        valid_records[0],
    )

    exit_code = cli.run(
        [
            "validate",
            "--source-root",
            str(source_root),
            "--data-root",
            str(tmp_path / "generated"),
        ]
    )

    assert exit_code == 0
    assert json.loads(capsys.readouterr().out) == {"files": 1, "rows": 1}


def test_validate_reports_unreadable_parquet_as_validation_error(
    tmp_path: Path, manifest_factory, capsys: pytest.CaptureFixture[str]
) -> None:
    parquet = _write_artifact_pair(tmp_path, tmp_path, manifest_factory)
    parquet.unlink()
    parquet.mkdir()

    exit_code = cli.run(["validate", "--data-root", str(tmp_path)])

    assert exit_code == cli.EXIT_VALIDATION
    error = capsys.readouterr().err
    assert "not a regular file" in error
    assert "Traceback" not in error


@pytest.mark.parametrize("entry_kind", ["parquet", "manifest"])
def test_validate_rejects_fifo_entries_without_blocking(
    tmp_path: Path, manifest_factory, entry_kind: str
) -> None:
    parquet = _write_artifact_pair(tmp_path, tmp_path, manifest_factory)
    entry = parquet if entry_kind == "parquet" else tmp_path / "manifests" / "a.manifest.json"
    entry.unlink()
    os.mkfifo(entry)

    result = subprocess.run(  # noqa: S603 - runs the local CLI fixture
        [
            sys.executable,
            "-m",
            "osm_polygon_description_tag.cli",
            "validate",
            "--data-root",
            str(tmp_path),
        ],
        capture_output=True,
        text=True,
        timeout=10,
        check=False,
    )

    assert result.returncode == cli.EXIT_VALIDATION
    assert "not a regular file" in result.stderr
    assert "Traceback" not in result.stderr


@pytest.mark.parametrize("entry_kind", ["parquet", "manifest"])
def test_validate_rejects_symlinked_entries(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    manifest_factory,
    capsys: pytest.CaptureFixture[str],
    entry_kind: str,
) -> None:
    monkeypatch.setattr(cli, "validate_geoparquet", lambda _path, **_kwargs: 1)
    parquet = _write_artifact_pair(tmp_path, tmp_path, manifest_factory)
    entry = parquet if entry_kind == "parquet" else tmp_path / "manifests" / "a.manifest.json"
    target = tmp_path / f"outside-{entry.name}"
    entry.rename(target)
    entry.symlink_to(target)

    exit_code = cli.run(["validate", "--data-root", str(tmp_path)])

    assert exit_code == cli.EXIT_VALIDATION
    error = capsys.readouterr().err
    assert "not a regular file" in error
    assert "Traceback" not in error


@pytest.mark.parametrize("directory_kind", ["data", "manifests"])
def test_validate_rejects_symlinked_artifact_directories(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    manifest_factory,
    capsys: pytest.CaptureFixture[str],
    directory_kind: str,
) -> None:
    monkeypatch.setattr(cli, "validate_geoparquet", lambda _path, **_kwargs: 1)
    _write_artifact_pair(tmp_path, tmp_path, manifest_factory)
    directory = tmp_path / directory_kind
    target = tmp_path / f"outside-{directory_kind}"
    directory.rename(target)
    directory.symlink_to(target, target_is_directory=True)

    exit_code = cli.run(["validate", "--data-root", str(tmp_path)])

    assert exit_code == cli.EXIT_VALIDATION
    error = capsys.readouterr().err
    assert "must be a real directory" in error
    assert "Traceback" not in error


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("schema_version", 999),
        ("geoparquet_version", "0.0.0"),
        ("transform_algorithm_version", 999),
        ("area_policy_sha256", "0" * 64),
        ("output_algorithm_revision", "0:0000000000000000"),
    ],
)
def test_validate_rejects_a_stale_manifest_contract(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    manifest_factory,
    capsys: pytest.CaptureFixture[str],
    field: str,
    value: object,
) -> None:
    monkeypatch.setattr(cli, "validate_geoparquet", lambda _path, **_kwargs: 1)
    _write_artifact_pair(tmp_path, tmp_path, manifest_factory)
    manifest_path = tmp_path / "manifests" / "a.manifest.json"
    payload = json.loads(manifest_path.read_text())
    payload[field] = value
    manifest_path.write_text(json.dumps(payload))

    exit_code = cli.run(["validate", "--data-root", str(tmp_path)])

    assert exit_code == cli.EXIT_VALIDATION
    error = capsys.readouterr().err
    assert "manifest contract does not match current configuration" in error
    assert "Traceback" not in error


@pytest.mark.parametrize(
    ("field", "value"),
    [("schema_version", "3"), ("transform_algorithm_version", "3")],
)
def test_validate_rejects_string_encoded_manifest_contract_versions(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    manifest_factory,
    capsys: pytest.CaptureFixture[str],
    field: str,
    value: str,
) -> None:
    monkeypatch.setattr(cli, "validate_geoparquet", lambda _path, **_kwargs: 1)
    _write_artifact_pair(tmp_path, tmp_path, manifest_factory)
    manifest_path = tmp_path / "manifests" / "a.manifest.json"
    payload = json.loads(manifest_path.read_text(encoding="utf-8"))
    payload[field] = value
    manifest_path.write_text(json.dumps(payload), encoding="utf-8")

    exit_code = cli.run(["validate", "--data-root", str(tmp_path)])

    assert exit_code == cli.EXIT_VALIDATION
    error = capsys.readouterr().err
    assert f"invalid manifest {field}" in error
    assert "non-negative integer" in error
    assert "Traceback" not in error


def test_validate_rejects_non_integer_manifest_schema_version(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    manifest_factory,
    capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.setattr(cli, "validate_geoparquet", lambda _path, **_kwargs: 1)
    _write_artifact_pair(tmp_path, tmp_path, manifest_factory)
    manifest_path = tmp_path / "manifests" / "a.manifest.json"
    payload = json.loads(manifest_path.read_text(encoding="utf-8"))
    payload["manifest_schema_version"] = 2.0
    manifest_path.write_text(json.dumps(payload), encoding="utf-8")

    exit_code = cli.run(["validate", "--data-root", str(tmp_path)])

    assert exit_code == cli.EXIT_VALIDATION
    error = capsys.readouterr().err
    assert "unsupported manifest schema version" in error
    assert "Traceback" not in error


@pytest.mark.parametrize(
    ("field", "value", "message"),
    [
        (
            "osmium_version",
            1,
            "invalid manifest osmium_version: expected a string or null",
        ),
        (
            "dependency_versions",
            [],
            "invalid manifest dependency_versions: expected string keys and values",
        ),
        (
            "dependency_versions",
            {"pyarrow": 1},
            "invalid manifest dependency_versions: expected string keys and values",
        ),
        (
            "code_revision",
            [],
            "invalid manifest code_revision: expected a string or null",
        ),
        (
            "started_at",
            123,
            "invalid manifest started_at: expected a string",
        ),
        (
            "completed_at",
            None,
            "invalid manifest completed_at: expected a string",
        ),
    ],
)
def test_validate_rejects_malformed_manifest_provenance_fields(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    manifest_factory,
    capsys: pytest.CaptureFixture[str],
    field: str,
    value: object,
    message: str,
) -> None:
    monkeypatch.setattr(cli, "validate_geoparquet", lambda _path, **_kwargs: 1)
    _write_artifact_pair(tmp_path, tmp_path, manifest_factory)
    manifest_path = tmp_path / "manifests" / "a.manifest.json"
    payload = json.loads(manifest_path.read_text(encoding="utf-8"))
    payload[field] = value
    manifest_path.write_text(json.dumps(payload), encoding="utf-8")

    exit_code = cli.run(["validate", "--data-root", str(tmp_path)])

    assert exit_code == cli.EXIT_VALIDATION
    error = capsys.readouterr().err
    assert message in error
    assert "Traceback" not in error


def test_validate_rejects_a_manifest_row_count_mismatch(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    manifest_factory,
    capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.setattr(cli, "validate_geoparquet", lambda _path, **_kwargs: 1)
    _write_artifact_pair(tmp_path, tmp_path, manifest_factory)
    manifest_path = tmp_path / "manifests" / "a.manifest.json"
    payload = json.loads(manifest_path.read_text())
    payload["counts"]["emitted_features"] = 2
    payload["counts"]["included_rows"] = 2
    manifest_path.write_text(json.dumps(payload))

    exit_code = cli.run(["validate", "--data-root", str(tmp_path)])

    assert exit_code == cli.EXIT_VALIDATION
    error = capsys.readouterr().err
    assert "manifest row count mismatch for a.parquet" in error
    assert "Traceback" not in error


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("emitted_features", 2),
        ("rejections", {"no_nonempty_description": 1}),
    ],
)
def test_validate_rejects_inconsistent_manifest_count_equation(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    manifest_factory,
    capsys: pytest.CaptureFixture[str],
    field: str,
    value: object,
) -> None:
    monkeypatch.setattr(cli, "validate_geoparquet", lambda _path, **_kwargs: 1)
    _write_artifact_pair(tmp_path, tmp_path, manifest_factory)
    manifest_path = tmp_path / "manifests" / "a.manifest.json"
    payload = json.loads(manifest_path.read_text())
    payload["counts"][field] = value
    manifest_path.write_text(json.dumps(payload))

    exit_code = cli.run(["validate", "--data-root", str(tmp_path)])

    assert exit_code == cli.EXIT_VALIDATION
    error = capsys.readouterr().err
    assert "manifest counts are inconsistent for a.parquet" in error
    assert "Traceback" not in error


def test_validate_accepts_manifest_counts_with_rejections(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    manifest_factory,
    capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.setattr(cli, "validate_geoparquet", lambda _path, **_kwargs: 1)
    _write_artifact_pair(tmp_path, tmp_path, manifest_factory)
    manifest_path = tmp_path / "manifests" / "a.manifest.json"
    payload = json.loads(manifest_path.read_text(encoding="utf-8"))
    payload["counts"] = {
        "emitted_features": 2,
        "included_rows": 1,
        "rejections": {"no_nonempty_description": 1},
    }
    manifest_path.write_text(json.dumps(payload), encoding="utf-8")

    exit_code = cli.run(["validate", "--data-root", str(tmp_path)])

    assert exit_code == 0
    assert json.loads(capsys.readouterr().out) == {"files": 1, "rows": 1}


def test_validate_requires_artifact_and_manifest_results_to_have_equal_lengths(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    (tmp_path / "data").mkdir()
    parquet = tmp_path / "data" / "a.parquet"
    manifest_path = tmp_path / "manifests" / "a.manifest.json"
    manifest = SimpleNamespace(
        source=SimpleNamespace(name="a.osm.pbf"),
        counts=SimpleNamespace(included_rows=1, emitted_features=1, rejections={}),
    )
    monkeypatch.setattr(
        cli,
        "validate_finalized_artifacts",
        lambda *_args, **_kwargs: {
            "parquets": (parquet, parquet),
            "manifests": (manifest_path,),
        },
    )
    monkeypatch.setattr(cli, "validate_geoparquet", lambda _path, **_kwargs: 1)
    monkeypatch.setattr(cli, "read_manifest", lambda _path: manifest)

    with pytest.raises(ValueError, match=r"zip\(\) argument 2 is shorter than argument 1"):
        cli.handle_validate(SimpleNamespace(data_root=tmp_path))


def test_validate_accepts_a_partial_set_with_matching_manifests(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, manifest_factory, capsys
) -> None:
    parquet = _write_artifact_pair(tmp_path, tmp_path, manifest_factory, "one-of-many")
    manifest_path = tmp_path / "manifests" / "one-of-many.manifest.json"
    payload = json.loads(manifest_path.read_text())
    payload["counts"]["emitted_features"] = 2
    payload["counts"]["included_rows"] = 2
    manifest_path.write_text(json.dumps(payload))
    monkeypatch.setattr(
        cli, "validate_geoparquet", lambda path, **_kwargs: 2 if path == parquet else 0
    )

    assert cli.handle_validate(SimpleNamespace(data_root=tmp_path)) == 0

    assert json.loads(capsys.readouterr().out) == {"files": 1, "rows": 2}
