import json
from pathlib import Path

import pytest

from osm_polygon_description_tag.manifest import (
    MANIFEST_SCHEMA_VERSION,
    Manifest,
    OutputIdentity,
    RunCounts,
    SourceIdentity,
    current_output_algorithm_revision,
    file_sha256,
    is_resumable,
    output_identity_for,
    read_manifest,
    source_identity_for,
    write_manifest,
)


def _identity(path: Path, *, sha: str = "deadbeef") -> SourceIdentity:
    return SourceIdentity(name=path.name, size_bytes=128, mtime_ns=1000, sha256=sha)


def test_sha256_reads_without_mutating(tmp_path: Path) -> None:
    path = tmp_path / "artifact"
    path.write_bytes(b"abc")
    before = path.stat()

    assert file_sha256(path) == "ba7816bf8f01cfea414140de5dae2223b00361a396177a9cb410ff61f20015ad"
    after = path.stat()
    assert after.st_mtime_ns == before.st_mtime_ns
    assert after.st_size == before.st_size


def test_source_and_output_identity_capture_checksums(tmp_path: Path) -> None:
    source = tmp_path / "region.osm.pbf"
    source.write_bytes(b"source-bytes")
    output = tmp_path / "data" / "region.parquet"
    output.parent.mkdir()
    output.write_bytes(b"output-bytes")

    src = source_identity_for(source)
    out = output_identity_for(output)

    assert src.name == "region.osm.pbf"
    assert src.size_bytes == source.stat().st_size
    assert src.mtime_ns == source.stat().st_mtime_ns
    assert src.sha256 == file_sha256(source)
    assert out.name == "region.parquet"
    assert out.size_bytes == output.stat().st_size
    assert out.sha256 == file_sha256(output)


def _manifest() -> Manifest:
    from osm_polygon_description_tag.manifest import (
        current_area_policy_sha256,
        current_output_algorithm_revision,
    )

    return Manifest(
        manifest_schema_version=MANIFEST_SCHEMA_VERSION,
        schema_version=2,
        geoparquet_version="1.1.0",
        transform_algorithm_version=2,
        area_policy_sha256=current_area_policy_sha256(),
        output_algorithm_revision=current_output_algorithm_revision(),
        source=SourceIdentity("region.osm.pbf", 128, 1000, "a" * 64),
        output=OutputIdentity("region.parquet", 4096, "b" * 64),
        osmium_version="osmium version 1.16.0",
        dependency_versions={"pyarrow": "20.0.0", "shapely": "2.1.0"},
        code_revision="0123abcd",
        started_at="2026-07-27T00:00:00+00:00",
        completed_at="2026-07-27T00:01:00+00:00",
        counts=RunCounts(
            emitted_features=10, included_rows=7, rejections={"no_nonempty_description": 3}
        ),
    )


def test_resumption_requires_matching_source_and_output() -> None:
    manifest = _manifest()

    # Ensure the project checkout revision is treated as either matching or
    # unavailable in the test environment.
    from osm_polygon_description_tag._resources import project_code_revision

    manifest = Manifest(
        manifest_schema_version=manifest.manifest_schema_version,
        schema_version=manifest.schema_version,
        geoparquet_version=manifest.geoparquet_version,
        transform_algorithm_version=manifest.transform_algorithm_version,
        output_algorithm_revision=current_output_algorithm_revision(),
        area_policy_sha256=manifest.area_policy_sha256,
        source=manifest.source,
        output=manifest.output,
        osmium_version=manifest.osmium_version,
        dependency_versions=manifest.dependency_versions,
        code_revision=project_code_revision(),
        started_at=manifest.started_at,
        completed_at=manifest.completed_at,
        counts=manifest.counts,
    )

    assert is_resumable(manifest, manifest.source, manifest.output)

    changed_source = SourceIdentity(
        manifest.source.name, 1, manifest.source.mtime_ns, manifest.source.sha256
    )
    assert not is_resumable(manifest, changed_source, manifest.output)

    changed_output = OutputIdentity(manifest.output.name, 1, manifest.output.sha256)
    assert not is_resumable(manifest, manifest.source, changed_output)


def test_resumption_rejects_unsupported_manifest_version() -> None:
    manifest = _manifest()
    old = Manifest(
        manifest_schema_version=MANIFEST_SCHEMA_VERSION + 1,
        schema_version=manifest.schema_version,
        geoparquet_version=manifest.geoparquet_version,
        transform_algorithm_version=manifest.transform_algorithm_version,
        output_algorithm_revision=current_output_algorithm_revision(),
        area_policy_sha256=manifest.area_policy_sha256,
        source=manifest.source,
        output=manifest.output,
        osmium_version=manifest.osmium_version,
        dependency_versions=manifest.dependency_versions,
        code_revision=manifest.code_revision,
        started_at=manifest.started_at,
        completed_at=manifest.completed_at,
        counts=manifest.counts,
    )
    assert not is_resumable(old, old.source, old.output)


def test_manifest_json_is_canonical_and_deterministic() -> None:
    payload_a = json.loads(_manifest().to_json())

    assert _manifest().to_json() == _manifest().to_json()
    assert payload_a["manifest_schema_version"] == MANIFEST_SCHEMA_VERSION
    assert payload_a["source"]["sha256"] == "a" * 64
    assert payload_a["counts"]["rejections"] == {"no_nonempty_description": 3}
    assert payload_a["geoparquet_version"] == "1.1.0"


def test_write_and_read_manifest_roundtrip(tmp_path: Path) -> None:
    manifest = _manifest()
    path = tmp_path / "region.manifest.json"

    write_manifest(manifest, path)

    assert path.is_file()
    assert path.read_text(encoding="utf-8").endswith("\n")
    restored = read_manifest(path)

    assert restored == manifest
    # Rebuild with the project checkout revision so resumption agrees.
    from osm_polygon_description_tag._resources import project_code_revision

    aligned = Manifest(
        manifest_schema_version=restored.manifest_schema_version,
        schema_version=restored.schema_version,
        geoparquet_version=restored.geoparquet_version,
        transform_algorithm_version=restored.transform_algorithm_version,
        area_policy_sha256=restored.area_policy_sha256,
        output_algorithm_revision=restored.output_algorithm_revision,
        source=restored.source,
        output=restored.output,
        osmium_version=restored.osmium_version,
        dependency_versions=restored.dependency_versions,
        code_revision=project_code_revision(),
        started_at=restored.started_at,
        completed_at=restored.completed_at,
        counts=restored.counts,
    )
    assert is_resumable(aligned, aligned.source, aligned.output)


def test_read_manifest_rejects_corrupt_json(tmp_path: Path) -> None:
    path = tmp_path / "bad.manifest.json"
    path.write_text("{not json", encoding="utf-8")

    with pytest.raises(ValueError, match="manifest"):
        read_manifest(path)


def test_read_manifest_rejects_unsupported_version(tmp_path: Path) -> None:
    path = tmp_path / "old.manifest.json"
    path.write_text(
        json.dumps({"manifest_schema_version": 999, "source": {}, "output": {}}) + "\n",
        encoding="utf-8",
    )

    with pytest.raises(ValueError, match="version"):
        read_manifest(path)


def test_write_manifest_is_atomic_and_never_partial(tmp_path: Path) -> None:
    manifest = _manifest()
    path = tmp_path / "region.manifest.json"

    write_manifest(manifest, path)

    # No leftover owned temporary files.
    assert list(tmp_path.glob(".*.tmp")) == []
    assert len(path.read_text(encoding="utf-8")) > 0
