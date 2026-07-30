from collections.abc import Iterable
from pathlib import Path
from typing import Any

import pytest
from shapely.geometry import Polygon

from osm_polygon_description_tag.config import Paths
from osm_polygon_description_tag.discovery import Source, discover_sources
from osm_polygon_description_tag.extraction import ExportRecord, OsmiumExportError
from osm_polygon_description_tag.manifest import read_manifest
from osm_polygon_description_tag.storage import write_geoparquet
from osm_polygon_description_tag.workflow.build import BuildResult, build_all, build_one
from tests.conftest import make_export_record


def _setup(tmp_path: Path) -> tuple[Paths, Source]:
    source_root = tmp_path / "raw"
    data_root = tmp_path / "generated"
    source_root.mkdir()
    (source_root / "region.osm.pbf").write_bytes(b"osm-pbf-bytes")
    paths = Paths(source_root=source_root, data_root=data_root)
    return paths, discover_sources(source_root)[0]


class _FakeWriter:
    def __init__(self) -> None:
        self.target: Path | None = None

    def __call__(
        self, records: Iterable[dict[str, object]], target: Path, batch_size: int = 1024
    ) -> int:
        self.target = target
        count = 0
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(b"fake-parquet")
        for _ in records:
            count += 1
        return count


def _described() -> ExportRecord:
    return make_export_record(
        Polygon([(0, 0), (0, 1), (1, 1), (1, 0)]), {"description": "x"}, osm_id=1
    )


def _undescribed() -> ExportRecord:
    return make_export_record(
        Polygon([(0, 0), (0, 1), (1, 1), (1, 0)]), {"name": "no desc"}, osm_id=2
    )


def _frozen_clock() -> str:
    return "2026-07-27T00:00:00+00:00"


def test_build_one_success_counts_included_and_rejected(tmp_path: Path) -> None:
    paths, source = _setup(tmp_path)
    fake_writer = _FakeWriter()

    result = build_one(
        source,
        paths,
        export_config=Path("config/osmium-export.json"),
        exporter=lambda *_: iter([_described(), _undescribed()]),
        writer=fake_writer,
        clock=_frozen_clock,
    )

    assert result.status == "built"
    assert result.included_rows == 1
    assert result.emitted_features == 2
    assert result.rejections == {"no_nonempty_description": 1}
    assert fake_writer.target == paths.data_root / "data" / source.output_name
    assert result.output_path == paths.data_root / "data" / source.output_name
    assert result.manifest_path == paths.data_root / "manifests" / "region.manifest.json"
    assert result.manifest_path.is_file()
    manifest = read_manifest(result.manifest_path)
    assert manifest.counts.rejections == {"no_nonempty_description": 1}
    assert manifest.counts.included_rows == 1


def test_build_one_outputs_never_under_source_root(tmp_path: Path) -> None:
    paths, source = _setup(tmp_path)

    build_one(
        source,
        paths,
        export_config=Path("config/osmium-export.json"),
        exporter=lambda *_: iter([_described()]),
        writer=_FakeWriter(),
        clock=_frozen_clock,
    )

    assert paths.source_root.resolve(strict=False) not in [
        result.resolve(strict=False) for result in (paths.data_root / "data").iterdir()
    ]


def test_build_one_skips_when_resumable(tmp_path: Path) -> None:
    paths, source = _setup(tmp_path)

    first = build_one(
        source,
        paths,
        export_config=Path("config/osmium-export.json"),
        exporter=lambda *_: iter([_described()]),
        writer=write_geoparquet,
        clock=_frozen_clock,
    )
    assert first.status == "built"

    second = build_one(
        source,
        paths,
        export_config=Path("config/osmium-export.json"),
        exporter=lambda *_: iter([_described()]),
        writer=write_geoparquet,
        clock=_frozen_clock,
    )
    assert second.status == "skipped"
    assert second.included_rows == first.included_rows


def test_build_one_rebuilds_when_source_changes(tmp_path: Path) -> None:
    paths, source = _setup(tmp_path)
    build_one(
        source,
        paths,
        export_config=Path("config/osmium-export.json"),
        exporter=lambda *_: iter([_described()]),
        writer=write_geoparquet,
        clock=_frozen_clock,
    )
    # Mutate the source identity.
    source.path.write_bytes(b"different-bytes")
    refreshed = discover_sources(paths.source_root)[0]

    result = build_one(
        refreshed,
        paths,
        export_config=Path("config/osmium-export.json"),
        exporter=lambda *_: iter([_described()]),
        writer=write_geoparquet,
        clock=_frozen_clock,
    )
    assert result.status == "built"


def test_build_one_exporter_failure_propagates(tmp_path: Path) -> None:
    paths, source = _setup(tmp_path)

    def failing_exporter(*_args: Any) -> Iterable[ExportRecord]:
        raise RuntimeError("osmium crashed")

    with pytest.raises(RuntimeError, match="osmium crashed"):
        build_one(
            source,
            paths,
            export_config=Path("config/osmium-export.json"),
            exporter=failing_exporter,
            writer=_FakeWriter(),
            clock=_frozen_clock,
        )
    assert not (paths.data_root / "data" / source.output_name).exists()


def test_build_all_runs_in_sorted_order_and_fail_fast(tmp_path: Path) -> None:
    seen: list[str] = []

    def build(source: Source) -> BuildResult:
        seen.append(source.name)
        return BuildResult(
            source_name=source.name,
            output_name=source.output_name,
            status="built",
            emitted_features=0,
            included_rows=0,
            rejections={},
            output_path=Path("/out") / source.output_name,
            manifest_path=Path("/out") / "manifests" / "x.manifest.json",
        )

    source_z = Source(Path("/raw/z.osm.pbf"), "z.osm.pbf", "z.parquet", 1, 1)
    source_a = Source(Path("/raw/a.osm.pbf"), "a.osm.pbf", "a.parquet", 1, 1)
    results = build_all((source_z, source_a), build=build)

    assert seen == ["a.osm.pbf", "z.osm.pbf"]
    assert [result.source_name for result in results] == seen


def test_build_all_stops_on_first_failure(tmp_path: Path) -> None:
    calls: list[str] = []

    def build(source: Source) -> BuildResult:
        calls.append(source.name)
        if source.name == "b.osm.pbf":
            raise RuntimeError("infrastructure failure")
        return BuildResult(
            source_name=source.name,
            output_name=source.output_name,
            status="built",
            emitted_features=0,
            included_rows=0,
            rejections={},
            output_path=Path("/out") / source.output_name,
            manifest_path=Path("/out") / "manifests" / "x.manifest.json",
        )

    sources = (
        Source(Path("/raw/a.osm.pbf"), "a.osm.pbf", "a.parquet", 1, 1),
        Source(Path("/raw/b.osm.pbf"), "b.osm.pbf", "b.parquet", 1, 1),
        Source(Path("/raw/c.osm.pbf"), "c.osm.pbf", "c.parquet", 1, 1),
    )
    with pytest.raises(RuntimeError, match="infrastructure failure"):
        build_all(sources, build=build)
    assert calls == ["a.osm.pbf", "b.osm.pbf"]


def test_build_one_default_exporter_attempts_real_osmium(tmp_path: Path) -> None:
    paths, source = _setup(tmp_path)

    with pytest.raises(OsmiumExportError):
        # No injected exporter: the default closure invokes stream_export, which
        # fails because the real osmium binary is not installed in the test env.
        build_one(
            source,
            paths,
            export_config=Path("config/osmium-export.json"),
            writer=_FakeWriter(),
            clock=_frozen_clock,
        )


def test_build_one_rejects_symlink_source(tmp_path: Path) -> None:
    source_root = tmp_path / "raw"
    data_root = tmp_path / "generated"
    source_root.mkdir()
    target = tmp_path / "external.osm.pbf"
    target.write_bytes(b"x")
    link = source_root / "region.osm.pbf"
    link.symlink_to(target)
    paths = Paths(source_root=source_root, data_root=data_root)
    source = Source(
        link,
        "region.osm.pbf",
        "region.parquet",
        link.stat().st_size,
        link.stat().st_mtime_ns,
    )

    with pytest.raises(Exception, match="symlink|regular"):
        build_one(
            source,
            paths,
            export_config=Path("config/osmium-export.json"),
            exporter=lambda *_: iter([_described()]),
            writer=_FakeWriter(),
            clock=_frozen_clock,
        )


def test_build_one_rejects_source_outside_root(tmp_path: Path) -> None:
    source_root = tmp_path / "raw"
    data_root = tmp_path / "generated"
    source_root.mkdir()
    foreign = tmp_path / "elsewhere.osm.pbf"
    foreign.write_bytes(b"x")
    paths = Paths(source_root=source_root, data_root=data_root)
    source = Source(foreign, "elsewhere.osm.pbf", "elsewhere.parquet", 1, 1)

    with pytest.raises(Exception, match="inside|source root"):
        build_one(
            source,
            paths,
            export_config=Path("config/osmium-export.json"),
            exporter=lambda *_: iter([_described()]),
            writer=_FakeWriter(),
            clock=_frozen_clock,
        )


def test_build_one_records_osmium_version_when_provided(tmp_path: Path) -> None:
    paths, source = _setup(tmp_path)
    fake_writer = _FakeWriter()

    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(
            "osm_polygon_description_tag.workflow.build.safe_osmium_version",
            lambda executable: f"osmium version test ({executable})",
        )
        result = build_one(
            source,
            paths,
            export_config=Path("config/osmium-export.json"),
            executable="my-osmium",
            exporter=lambda *_: iter([_described()]),
            writer=fake_writer,
            clock=_frozen_clock,
        )

    manifest = read_manifest(result.manifest_path)
    assert manifest.osmium_version == "osmium version test (my-osmium)"
