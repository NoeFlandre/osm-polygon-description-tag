"""Legacy Arrow-map migration tests."""

import json
import re
from pathlib import Path

import pyarrow as pa
import pyarrow.parquet as pq
import pytest
from shapely import to_wkb
from shapely.geometry import Polygon

from osm_polygon_description_tag.dataset import migration
from osm_polygon_description_tag.dataset.manifest import (
    MANIFEST_SCHEMA_VERSION,
    Manifest,
    OutputIdentity,
    RunCounts,
    SourceIdentity,
    current_area_policy_sha256,
    current_output_algorithm_revision,
    output_identity_for,
    read_manifest,
    write_manifest,
)
from osm_polygon_description_tag.dataset.migration import (
    MigrationError,
    migrate_dataset_schema,
)
from osm_polygon_description_tag.dataset.schema import SCHEMA, SCHEMA_VERSION
from osm_polygon_description_tag.dataset.storage import StorageError


def _legacy_schema() -> pa.Schema:
    return pa.schema(
        [
            pa.field(field.name, pa.map_(pa.string(), pa.string()))
            if field.name in {"localized_names", "localized_descriptions", "tags"}
            else field
            for field in SCHEMA
        ]
    )


def _legacy_row() -> dict[str, object]:
    return {
        "source_pbf": "region.osm.pbf",
        "osm_type": "way",
        "osm_id": 1,
        "osm_url": "https://www.openstreetmap.org/way/1",
        "version": 1,
        "changeset": 1,
        "timestamp": None,
        "name": "Example",
        "localized_names": {"en": "Example"},
        "description": "A polygon",
        "localized_descriptions": {"fr": "Un polygone"},
        "tags": {"description": "A polygon", "name": "Example"},
        "geometry_type": "Polygon",
        "area_m2": 1.0,
        "bbox_min_x": 0.0,
        "bbox_min_y": 0.0,
        "bbox_max_x": 1.0,
        "bbox_max_y": 1.0,
        "geometry": to_wkb(Polygon([(0, 0), (0, 1), (1, 1), (1, 0)])),
    }


def _write_legacy_parquet(path: Path) -> None:
    pq.write_table(
        pa.Table.from_pylist([_legacy_row()], schema=_legacy_schema()),
        path,
        compression="zstd",
    )


def _manifest_for(schema_version: int) -> Manifest:
    return Manifest(
        manifest_schema_version=MANIFEST_SCHEMA_VERSION,
        schema_version=schema_version,
        geoparquet_version="1.1.0",
        transform_algorithm_version=2,
        area_policy_sha256=current_area_policy_sha256(),
        output_algorithm_revision="old-revision",
        source=SourceIdentity("region.osm.pbf", 1, 1, "a" * 64),
        output=OutputIdentity("region.parquet", 1, "b" * 64),
        osmium_version=None,
        dependency_versions={},
        code_revision=None,
        started_at="2026-01-01T00:00:00+00:00",
        completed_at="2026-01-01T00:00:00+00:00",
        counts=RunCounts(1, 1, {}),
    )


class _ReverseOrderedPath:
    def __init__(self, name: str) -> None:
        self.name = name
        self.stem = name.removesuffix(".parquet")

    def __lt__(self, other: object) -> bool:
        if not isinstance(other, _ReverseOrderedPath):
            return NotImplemented
        return self.name > other.name


def test_migrate_legacy_maps_updates_parquet_and_manifest(tmp_path: Path) -> None:
    data_root = tmp_path
    data_dir = data_root / "data"
    manifests_dir = data_root / "manifests"
    data_dir.mkdir()
    manifests_dir.mkdir()
    parquet = data_dir / "region.parquet"

    fields = [
        pa.field(field.name, pa.map_(pa.string(), pa.string()))
        if field.name in {"localized_names", "localized_descriptions", "tags"}
        else field
        for field in SCHEMA
    ]
    legacy_schema = pa.schema(fields)
    row = {
        "source_pbf": "region.osm.pbf",
        "osm_type": "way",
        "osm_id": 1,
        "osm_url": "https://www.openstreetmap.org/way/1",
        "version": 1,
        "changeset": 1,
        "timestamp": None,
        "name": "Example",
        "localized_names": {"en": "Example"},
        "description": "A polygon",
        "localized_descriptions": {"fr": "Un polygone"},
        "tags": {"description": "A polygon", "name": "Example"},
        "geometry_type": "Polygon",
        "area_m2": 1.0,
        "bbox_min_x": 0.0,
        "bbox_min_y": 0.0,
        "bbox_max_x": 1.0,
        "bbox_max_y": 1.0,
        "geometry": to_wkb(Polygon([(0, 0), (0, 1), (1, 1), (1, 0)])),
    }
    table = pa.Table.from_pylist([row], schema=legacy_schema)
    metadata = legacy_schema.with_metadata(
        {
            b"geo": json.dumps(
                {
                    "version": "1.1.0",
                    "primary_column": "geometry",
                    "columns": {
                        "geometry": {
                            "encoding": "WKB",
                            "geometry_types": ["Polygon"],
                            "bbox": [0.0, 0.0, 1.0, 1.0],
                        }
                    },
                }
            ).encode()
        }
    )
    pq.write_table(table.cast(metadata), parquet, compression="zstd")

    manifest = Manifest(
        manifest_schema_version=MANIFEST_SCHEMA_VERSION,
        schema_version=2,
        geoparquet_version="1.1.0",
        transform_algorithm_version=2,
        area_policy_sha256=current_area_policy_sha256(),
        output_algorithm_revision=current_output_algorithm_revision(),
        source=SourceIdentity("region.osm.pbf", 1, 1, "a" * 64),
        output=output_identity_for(parquet),
        osmium_version=None,
        dependency_versions={},
        code_revision=None,
        started_at="2026-01-01T00:00:00+00:00",
        completed_at="2026-01-01T00:00:00+00:00",
        counts=RunCounts(1, 1, {}),
    )
    manifest_path = manifests_dir / "region.manifest.json"
    write_manifest(manifest, manifest_path)

    assert migrate_dataset_schema(data_root) == 1
    migrated_schema = pq.read_schema(parquet)
    assert migrated_schema.names == SCHEMA.names
    assert migrated_schema.field("tags").type == SCHEMA.field("tags").type
    assert pq.read_table(parquet).column("tags").to_pylist() == [
        [{"key": "description", "value": "A polygon"}, {"key": "name", "value": "Example"}]
    ]
    migrated_manifest = read_manifest(manifest_path)
    assert migrated_manifest.schema_version == SCHEMA_VERSION
    assert migrated_manifest.transform_algorithm_version == 3
    assert migrated_manifest.output == output_identity_for(parquet)
    assert migrate_dataset_schema(data_root) == 0


def test_requires_migration_reports_the_unsupported_path() -> None:
    unsupported = pa.schema([pa.field("unexpected", pa.int8())])
    path = Path("unsupported.parquet")

    with pytest.raises(MigrationError, match=re.escape(str(path))):
        migration._requires_migration(unsupported, path)


def test_migrate_parquet_reports_path_when_schema_is_unsupported(tmp_path: Path) -> None:
    path = tmp_path / "unsupported.parquet"
    pq.write_table(pa.table({"unexpected": [1]}), path)

    with pytest.raises(MigrationError, match=re.escape(str(path))):
        migration._migrate_parquet(path)


def test_migrate_parquet_returns_true_after_promoting_a_migration(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "legacy.parquet"
    _write_legacy_parquet(path)
    monkeypatch.setattr(migration, "_rewrite_legacy_parquet", lambda *_args: None)
    monkeypatch.setattr(migration, "_promote_migrated_parquet", lambda *_args: None)

    assert migration._migrate_parquet(path) is True


def test_migrate_parquet_wraps_storage_errors_with_the_artifact_path(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "legacy.parquet"
    _write_legacy_parquet(path)

    def fail(*_args: object) -> None:
        raise StorageError("invalid migrated output")

    monkeypatch.setattr(migration, "_rewrite_legacy_parquet", fail)

    with pytest.raises(MigrationError, match=re.escape(str(path))):
        migration._migrate_parquet(path)


def test_rewrite_legacy_parquet_requests_zstd_and_bounded_batches(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    observed: dict[str, object] = {}

    class Reader:
        def iter_batches(self, *, batch_size: int) -> tuple[object, ...]:
            observed["batch_size"] = batch_size
            return ()

    class Writer:
        def __init__(self, _path: Path, _schema: pa.Schema, **kwargs: object) -> None:
            observed["compression"] = kwargs.get("compression")

        def __enter__(self) -> "Writer":
            return self

        def __exit__(self, *_args: object) -> None:
            return None

        def write_table(self, _table: pa.Table) -> None:
            raise AssertionError("empty reader should not write a table")

    monkeypatch.setattr(migration.pq, "ParquetWriter", Writer)
    monkeypatch.setattr(migration, "validate_geoparquet", lambda _path: None)

    migration._rewrite_legacy_parquet(Reader(), tmp_path / "temporary.parquet", SCHEMA)

    assert observed == {"batch_size": 4096, "compression": "zstd"}


def test_promote_migrated_parquet_fsyncs_a_binary_handle_before_replace(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    temporary = tmp_path / "temporary.parquet"
    target = tmp_path / "target.parquet"
    observed: dict[str, object] = {}

    class Handle:
        def __enter__(self) -> "Handle":
            return self

        def __exit__(self, *_args: object) -> None:
            return None

        def fileno(self) -> int:
            return 17

    def fake_open(path: Path, mode: str = "r") -> Handle:
        observed["open"] = (path, mode)
        return Handle()

    monkeypatch.setattr(migration, "open", fake_open, raising=False)
    monkeypatch.setattr(
        migration.os, "fsync", lambda descriptor: observed.setdefault("fsync", descriptor)
    )
    monkeypatch.setattr(
        migration.os,
        "replace",
        lambda source, destination: observed.setdefault("replace", (source, destination)),
    )

    migration._promote_migrated_parquet(temporary, target)

    assert observed == {
        "open": (temporary, "rb"),
        "fsync": 17,
        "replace": (temporary, target),
    }


@pytest.mark.parametrize("missing", ["data", "manifests"])
def test_migration_requires_both_directories(tmp_path: Path, missing: str) -> None:
    data_dir = tmp_path / "data"
    manifests_dir = tmp_path / "manifests"
    (manifests_dir if missing == "data" else data_dir).mkdir()

    with pytest.raises(MigrationError, match=re.escape(str(tmp_path))):
        migration._require_migration_directories(data_dir, manifests_dir, tmp_path)


def test_migrate_dataset_schema_sorts_by_name_and_accumulates_results(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    (tmp_path / "data").mkdir()
    (tmp_path / "manifests").mkdir()
    seen: list[tuple[str, str]] = []

    def fake_glob(_directory: Path, pattern: str) -> list[_ReverseOrderedPath]:
        assert pattern == "*.parquet"
        return [_ReverseOrderedPath("b.parquet"), _ReverseOrderedPath("a.parquet")]

    def fake_migrate_one(parquet: _ReverseOrderedPath, manifest_path: Path) -> int:
        seen.append((parquet.name, manifest_path.name))
        return 1

    monkeypatch.setattr(Path, "glob", fake_glob)
    monkeypatch.setattr(migration, "_migrate_one_artifact", fake_migrate_one)

    assert migration.migrate_dataset_schema(tmp_path) == 2
    assert seen == [
        ("a.parquet", "a.manifest.json"),
        ("b.parquet", "b.manifest.json"),
    ]


@pytest.mark.parametrize(
    ("changed", "schema_version"),
    [(True, SCHEMA_VERSION), (False, SCHEMA_VERSION - 1)],
)
def test_migrate_one_artifact_refreshes_changed_or_stale_manifests(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    changed: bool,
    schema_version: int,
) -> None:
    parquet = tmp_path / "region.parquet"
    manifest_path = tmp_path / "region.manifest.json"
    written: list[tuple[Manifest, Path]] = []
    manifest = _manifest_for(schema_version)
    expected_output = OutputIdentity("region.parquet", 2, "c" * 64)

    monkeypatch.setattr(migration, "_migrate_parquet", lambda _path: changed)
    monkeypatch.setattr(migration, "read_manifest", lambda _path: manifest)
    monkeypatch.setattr(migration, "output_identity_for", lambda _path: expected_output)
    monkeypatch.setattr(
        migration,
        "current_output_algorithm_revision",
        lambda: "current-revision",
    )
    monkeypatch.setattr(
        migration,
        "write_manifest",
        lambda value, path: written.append((value, path)),
    )

    assert migration._migrate_one_artifact(parquet, manifest_path) == 1
    assert len(written) == 1
    assert written[0][1] == manifest_path
    assert written[0][0].output_algorithm_revision == "current-revision"
    assert written[0][0].output == expected_output


def test_migrate_dataset_schema_uses_the_lowercase_data_boundaries(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class Directory:
        def __init__(self, name: str) -> None:
            self.name = name

        def glob(self, pattern: str) -> tuple[object, ...]:
            assert pattern == "*.parquet"
            return ()

    class Root:
        def __truediv__(self, name: str) -> Directory:
            return Directory(name)

    observed: dict[str, object] = {}

    def fake_require(data_dir: Directory, manifests_dir: Directory, data_root: Root) -> None:
        observed.update(
            data_dir=data_dir.name,
            manifests_dir=manifests_dir.name,
            data_root=data_root,
        )

    root = Root()
    monkeypatch.setattr(migration, "_require_migration_directories", fake_require)

    assert migration.migrate_dataset_schema(root) == 0
    assert observed == {"data_dir": "data", "manifests_dir": "manifests", "data_root": root}
