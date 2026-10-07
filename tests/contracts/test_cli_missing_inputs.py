"""Public CLI contracts for missing source and validation inputs."""

from pathlib import Path

import pytest

from osm_polygon_description_tag.cli import run


def test_build_one_reports_an_undiscovered_source_as_an_environment_error(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    source_root = tmp_path / "raw"
    source_root.mkdir()
    (source_root / "other.osm.pbf").write_bytes(b"unused source")
    data_root = tmp_path / "generated"

    code = run(
        [
            "build-one",
            "missing.osm.pbf",
            "--source-root",
            str(source_root),
            "--data-root",
            str(data_root),
        ]
    )

    captured = capsys.readouterr()
    assert code == 3
    assert captured.out == ""
    assert "source not discovered: missing.osm.pbf" in captured.err
    assert "Traceback" not in captured.err
    assert not data_root.exists()


@pytest.mark.parametrize("data_path_is_file", [False, True])
def test_validate_reports_a_missing_data_directory_as_a_validation_error(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
    data_path_is_file: bool,
) -> None:
    monkeypatch.delenv("OSM_POLYGON_SOURCE_ROOT", raising=False)
    data_dir = tmp_path / "data"
    if data_path_is_file:
        data_dir.write_bytes(b"not a directory")

    code = run(["validate", "--data-root", str(tmp_path)])

    captured = capsys.readouterr()
    assert code == 4
    assert captured.out == ""
    assert f"missing data directory: {data_dir}" in captured.err
    assert "Traceback" not in captured.err
