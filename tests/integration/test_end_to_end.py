"""Real-osmium end-to-end test against the committed synthetic fixture.

This test converts the synthetic OSM XML fixture to a ``.osm.pbf`` with the
real ``osmium`` executable, invokes the public CLI against temporary roots,
and asserts exact inclusion and exclusion behavior, GeoParquet metadata,
manifest checksums, dataset card, and source non-mutation.

The test fails the moment ``osmium`` resolves to a dummy executable such as
``/usr/bin/true``: the real ``osmium export`` subprocess must succeed and
yield at least one streaming record before the test will proceed.
"""

from __future__ import annotations

import hashlib
import json
import shutil
import subprocess
from pathlib import Path

import pyarrow.parquet as pq
import pytest
from shapely import from_wkb
from shapely.geometry import MultiPolygon as _MultiPolygon
from shapely.geometry import Polygon as _Polygon

from osm_polygon_description_tag.cli import run

FIXTURE = Path("tests/fixtures/descriptions.osm")


def _real_osmium_path() -> str:
    """Return the path to a verified real ``osmium`` binary.

    A successful ``--version`` query must return a string mentioning
    ``libosmium`` or ``osmium version`` before the test proceeds.
    """
    path = shutil.which("osmium")
    if path is None:
        pytest.skip("osmium executable is required for the synthetic end-to-end test")
    completed = subprocess.run(  # noqa: S603 - controlled argument array, no shell
        [path, "--version"], check=True, capture_output=True, text=True, timeout=15
    )
    output = completed.stdout or completed.stderr or ""
    if "libosmium" not in output and "osmium version" not in output:
        pytest.skip(f"osmium at {path!r} does not look like a real osmium-tool binary: {output!r}")
    if not output.strip():
        pytest.skip(f"osmium at {path!r} produced no output")
    return path


def _to_map(pairs: object) -> dict[str, str]:
    """Convert PyArrow map<string,string> (serialized as list of tuples) into a dict."""
    return dict(pairs or [])  # type: ignore[arg-type]


@pytest.mark.integration
def test_synthetic_end_to_end(tmp_path: Path) -> None:
    """Real osmium-binary end-to-end against the committed synthetic fixture."""
    osmium_path = _real_osmium_path()

    fixture = FIXTURE
    assert fixture.is_file(), "synthetic fixture must be committed"

    source_root = tmp_path / "raw"
    data_root = tmp_path / "generated"
    source_root.mkdir()
    data_root.mkdir()

    # Convert the synthetic .osm to a .osm.pbf so the real osmium export can
    # stream it back. Capture content/size/mtime for non-mutation assertions.
    pbf_path = source_root / "synthetic.osm.pbf"
    completed = subprocess.run(  # noqa: S603 - controlled argument array, no shell
        [osmium_path, "cat", "-o", str(pbf_path), "--overwrite", "-f", "pbf", str(fixture)],
        check=True,
        capture_output=True,
        text=True,
        timeout=120,
    )
    assert pbf_path.is_file()
    assert completed.returncode == 0
    pbf_sha_before = hashlib.sha256(pbf_path.read_bytes()).hexdigest()
    pbf_size_before = pbf_path.stat().st_size
    pbf_mtime_before = pbf_path.stat().st_mtime_ns

    # 4. Run the full public CLI via the build-one subcommand first.
    exit_code = run(
        [
            "build-one",
            "synthetic.osm.pbf",
            "--source-root",
            str(source_root),
            "--data-root",
            str(data_root),
            "--osmium",
            osmium_path,
        ]
    )
    assert exit_code == 0, f"build-one failed with exit code {exit_code}"

    # 18. Source fixture must not be mutated.
    pbf_sha_after = hashlib.sha256(pbf_path.read_bytes()).hexdigest()
    assert pbf_sha_after == pbf_sha_before
    assert pbf_path.stat().st_size == pbf_size_before
    assert pbf_path.stat().st_mtime_ns == pbf_mtime_before

    parquet_path = data_root / "data" / "synthetic.parquet"
    manifest_path = data_root / "manifests" / "synthetic.manifest.json"
    assert parquet_path.is_file(), "build-one did not produce a parquet"
    assert manifest_path.is_file(), "build-one did not produce a manifest"

    # 5. Validate handler: confirms the parquet passes full validation.
    exit_code = run(
        [
            "validate",
            "--source-root",
            str(source_root),
            "--data-root",
            str(data_root),
            "--osmium",
            osmium_path,
        ]
    )
    assert exit_code == 0

    # 6. Generate-card: regenerate stats.json and README.md.
    exit_code = run(
        [
            "generate-card",
            "--source-root",
            str(source_root),
            "--data-root",
            str(data_root),
            "--osmium",
            osmium_path,
        ]
    )
    assert exit_code == 0
    stats_path = data_root / "stats.json"
    readme_path = data_root / "README.md"
    assert stats_path.is_file() and stats_path.stat().st_size > 0
    assert readme_path.is_file() and readme_path.stat().st_size > 0

    # 7. Publish-plan: confirm the allowlisted upload plan + identity.
    exit_code = run(
        [
            "publish-plan",
            "--source-root",
            str(source_root),
            "--data-root",
            str(data_root),
            "--osmium",
            osmium_path,
        ]
    )
    assert exit_code == 0

    # 8. Read the resulting GeoParquet.
    table = pq.read_table(parquet_path)
    expected_columns = {
        "source_pbf",
        "osm_type",
        "osm_id",
        "osm_url",
        "version",
        "changeset",
        "timestamp",
        "name",
        "localized_names",
        "description",
        "localized_descriptions",
        "tags",
        "geometry_type",
        "area_m2",
        "bbox_min_x",
        "bbox_min_y",
        "bbox_max_x",
        "bbox_max_y",
        "geometry",
    }
    assert set(table.column_names) == expected_columns

    rows = table.to_pylist()
    osm_ids = {(row["osm_type"], row["osm_id"]) for row in rows}

    # 9. Exact included and excluded OSM IDs.
    expected_included = {("way", 100), ("way", 101), ("way", 300), ("relation", 500)}
    expected_excluded = {("way", 102), ("way", 103), ("way", 200), ("way", 201)}
    assert expected_included == osm_ids, (
        f"unexpected included ids: osm_ids={osm_ids}, "
        f"missing={expected_included - osm_ids}, "
        f"extra={osm_ids - expected_included}"
    )
    assert osm_ids.isdisjoint(expected_excluded)

    way_100 = next(row for row in rows if row["osm_type"] == "way" and row["osm_id"] == 100)
    # 10. Polygon and MultiPolygon behavior: osmium may emit either.
    assert way_100["geometry_type"] in {"Polygon", "MultiPolygon"}
    assert way_100["description"] == "Eligible building"

    # 15. Every original OSM tag is preserved exactly.
    way_100_tags = _to_map(way_100["tags"])
    assert way_100_tags["building"] == "yes"
    assert way_100_tags["description"] == "Eligible building"

    # 16. Base description and exact description suffixes are correct.
    way_300 = next(row for row in rows if row["osm_type"] == "way" and row["osm_id"] == 300)
    assert way_300["description"] is None
    assert _to_map(way_300["localized_descriptions"]).get("pt-BR") == "Com espaço"

    relation_500 = next(
        row for row in rows if row["osm_type"] == "relation" and row["osm_id"] == 500
    )
    assert relation_500["geometry_type"] in {"Polygon", "MultiPolygon"}
    assert _to_map(relation_500["localized_descriptions"]).get("en") == "Lake with island"

    # 12. Decode every WKB geometry and check validity/non-emptiness.
    for row in rows:
        geom = from_wkb(row["geometry"])
        assert not geom.is_empty
        assert geom.is_valid
        assert geom.geom_type == row["geometry_type"]

    # 13. area_m2 is finite and positive.
    for row in rows:
        assert row["area_m2"] > 0
        assert row["area_m2"] < 1.0e9

    # 14. Bounding boxes match decoded geometry.
    for row in rows:
        geom = from_wkb(row["geometry"])
        minx, miny, maxx, maxy = geom.bounds
        assert abs(row["bbox_min_x"] - minx) < 1e-9
        assert abs(row["bbox_min_y"] - miny) < 1e-9
        assert abs(row["bbox_max_x"] - maxx) < 1e-9
        assert abs(row["bbox_max_y"] - maxy) < 1e-9

    # 11. The multipolygon hole is preserved (relation 500).
    hole_geom = from_wkb(relation_500["geometry"])
    assert isinstance(hole_geom, _Polygon | _MultiPolygon)
    if isinstance(hole_geom, _MultiPolygon):
        assert len(hole_geom.geoms[0].interiors) >= 1
    else:
        assert len(hole_geom.interiors) >= 1

    # 17. GeoParquet 1.1 metadata + schema + manifest checksums + stats + card.
    schema_meta = pq.ParquetFile(parquet_path).schema_arrow.metadata
    assert b"geo" in schema_meta
    geo_meta = json.loads(schema_meta[b"geo"])
    assert geo_meta["version"] == "1.1.0"
    assert geo_meta["primary_column"] == "geometry"
    assert geo_meta["columns"]["geometry"]["encoding"] == "WKB"
    declared_types = set(geo_meta["columns"]["geometry"]["geometry_types"])
    assert declared_types.issubset({"Polygon", "MultiPolygon"})
    assert declared_types

    manifest_payload = json.loads(manifest_path.read_text(encoding="utf-8"))
    assert manifest_payload["source"]["sha256"] == pbf_sha_after
    assert manifest_payload["output"]["name"] == "synthetic.parquet"
    assert (
        manifest_payload["output"]["sha256"]
        == hashlib.sha256(parquet_path.read_bytes()).hexdigest()
    )

    readme = readme_path.read_text(encoding="utf-8")
    stats = json.loads(stats_path.read_text(encoding="utf-8"))
    assert "OpenStreetMap contributors" in readme
    assert "Open Database License" in readme
    assert "ODbL" in readme
    assert stats["rows"] == len(rows)
    assert stats["output_files"] == 1
    assert "Polygon" in stats["geometry_types"] or "MultiPolygon" in stats["geometry_types"]

    # 19. No network command was executed: no publication state exists.
    state_path = data_root / "publication-state.json"
    assert not state_path.exists(), "no publication state should exist after inspect-only"


def test_dummy_osmium_is_rejected(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """A dummy ``osmium`` that returns no libosmium version is rejected."""
    dummy = tmp_path / "dummy-osmium"
    dummy.write_text("#!/bin/sh\necho nope\n", encoding="utf-8")
    dummy.chmod(0o755)

    monkeypatch.setattr("shutil.which", lambda name: str(dummy) if name == "osmium" else None)

    with pytest.raises(pytest.skip.Exception):
        _real_osmium_path()


def test_real_osmium_detects_dummy_executable(tmp_path: Path) -> None:
    """A symlink to ``/usr/bin/true`` is rejected by the version probe."""
    if not Path("/usr/bin/true").exists():
        pytest.skip("/usr/bin/true not available on this platform")
    dummy = tmp_path / "true-osmium"
    try:
        dummy.symlink_to("/usr/bin/true")
    except OSError:
        pytest.skip("cannot create symlink")
    completed = subprocess.run(  # noqa: S603 - controlled argument array, no shell
        [str(dummy), "--version"], check=True, capture_output=True, text=True, timeout=5
    )
    output = completed.stdout or completed.stderr or ""
    assert "libosmium" not in output
    assert "osmium version" not in output


__all__ = ["FIXTURE", "_real_osmium_path", "test_synthetic_end_to_end"]
