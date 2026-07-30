"""RED tests for the production preflight contract.

The preflight must:

- run ``osmium --version`` and reject any binary without ``libosmium`` or
  ``osmium version`` markers;
- run ``hf auth whoami`` and confirm authentication;
- verify that the authenticated identity can write the target dataset
  repository (e.g. via a Hub API write check);
- require the exact ``--confirm-repo`` value;
- never be silently skipped.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from osm_polygon_description_tag.config import Paths
from osm_polygon_description_tag.workflow.preflight import (
    PreflightError,
    default_preflight,
)


def _paths(tmp_path: Path) -> Paths:
    source_root = tmp_path / "raw"
    data_root = tmp_path / "generated"
    source_root.mkdir()
    data_root.mkdir()
    (source_root / "a.osm.pbf").write_bytes(b"a-bytes")
    return Paths(source_root=source_root, data_root=data_root)


def test_dummy_osmium_is_rejected(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """A dummy executable named ``osmium`` must be rejected by preflight."""
    paths = _paths(tmp_path)
    dummy = tmp_path / "dummy-osmium"
    dummy.write_text("#!/bin/sh\necho nope\n", encoding="utf-8")
    dummy.chmod(0o755)

    real_which_calls: list[str] = []

    def fake_which(name: str) -> str | None:
        real_which_calls.append(name)
        if name == "osmium":
            return str(dummy)
        # Allow the real shutil.which to handle other names.
        import shutil as _shutil

        return _shutil.which(name)

    monkeypatch.setattr("shutil.which", fake_which)

    # We also need to bypass the hf whoami check; provide a fake hf binary.
    fake_hf = tmp_path / "fake-hf"
    fake_hf.write_text("#!/bin/sh\necho test-user\n", encoding="utf-8")
    fake_hf.chmod(0o755)

    import shutil as _shutil

    monkeypatch.setattr(
        _shutil,
        "which",
        lambda name: (str(dummy) if name == "osmium" else str(fake_hf) if name == "hf" else None),
    )

    with pytest.raises(PreflightError, match="osmium"):
        default_preflight(
            paths,
            confirm_repo="NoeFlandre/osm-polygon-description-tag",
            osmium_executable="osmium",
            hf_executable="hf",
        )


def test_preflight_reports_osmium_version(tmp_path: Path) -> None:
    """The preflight report includes the verified osmium version line."""
    paths = _paths(tmp_path)

    report = default_preflight(
        paths,
        confirm_repo="NoeFlandre/osm-polygon-description-tag",
        osmium_executable="osmium",
        hf_executable="hf",
    )
    assert "osmium_version" in report
    assert report["osmium_version"] != ""
    assert "libosmium" in report["osmium_version"] or "osmium version" in report["osmium_version"]


def test_preflight_reports_required_identifiers(tmp_path: Path) -> None:
    """The preflight report includes all required identifiers."""
    paths = _paths(tmp_path)

    report = default_preflight(
        paths,
        confirm_repo="NoeFlandre/osm-polygon-description-tag",
        osmium_executable="osmium",
        hf_executable="hf",
    )
    for key in (
        "osmium_version",
        "hf_whoami",
        "repo_id",
        "source_count",
        "source_root",
        "data_root",
        "area_policy_sha256",
        "transform_algorithm_version",
    ):
        assert key in report, f"missing key in preflight report: {key}"


def test_preflight_requires_exact_confirm_repo(tmp_path: Path) -> None:
    """The preflight must refuse anything other than the exact repo id."""
    paths = _paths(tmp_path)

    with pytest.raises(PreflightError, match="confirm-repo|confirm_repo"):
        default_preflight(
            paths,
            confirm_repo="some-other/repo",
            osmium_executable="osmium",
            hf_executable="hf",
        )


def test_preflight_requires_hf_whoami_success(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A failed ``hf auth whoami`` must raise PreflightError."""
    paths = _paths(tmp_path)
    import subprocess

    real_run = subprocess.run

    def fake_run(command, *args, **kwargs):
        if Path(command[0]).name == "hf" and command[1:3] == ["auth", "whoami"]:
            raise subprocess.CalledProcessError(1, command, stderr=b"login required")
        return real_run(command, *args, **kwargs)

    monkeypatch.setattr("osm_polygon_description_tag.workflow.preflight.subprocess.run", fake_run)

    with pytest.raises(PreflightError, match="hf authentication"):
        default_preflight(
            paths,
            confirm_repo="NoeFlandre/osm-polygon-description-tag",
            osmium_executable="osmium",
            hf_executable="hf",
        )


def test_preflight_refuses_when_path_containment_fails(tmp_path: Path) -> None:
    """Paths whose data_root is inside the source_root are refused."""
    source_root = tmp_path / "raw"
    data_root = source_root / "nested"
    source_root.mkdir()
    data_root.mkdir()
    (source_root / "a.osm.pbf").write_bytes(b"a-bytes")
    paths = Paths(source_root=source_root, data_root=data_root)

    with pytest.raises(PreflightError):
        default_preflight(
            paths,
            confirm_repo="NoeFlandre/osm-polygon-description-tag",
            osmium_executable="osmium",
            hf_executable="hf",
        )
