from __future__ import annotations

import inspect
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, call, patch

import pytest

import osm_polygon_description_tag.workflow.build as build_module
from osm_polygon_description_tag.config import Paths
from osm_polygon_description_tag.dataset.transform import RejectedFeature
from osm_polygon_description_tag.discovery import Source
from osm_polygon_description_tag.extraction import ExportRecord


def _source(tmp_path: Path) -> Source:
    source_root = tmp_path / "raw"
    source_root.mkdir(exist_ok=True)
    path = source_root / "region.osm.pbf"
    path.write_bytes(b"source")
    return Source(path, path.name, "region.parquet", 6, 1)


def test_safe_osmium_version_returns_version_or_none() -> None:
    with patch.object(build_module, "osmium_version", return_value="osmium 1.19") as version:
        assert build_module.safe_osmium_version("osmium") == "osmium 1.19"
    version.assert_called_once_with("osmium")

    with patch.object(
        build_module,
        "osmium_version",
        side_effect=build_module.OsmiumExportError("missing"),
    ):
        assert build_module.safe_osmium_version("missing-osmium") is None


def test_verify_direct_child_checks_non_symlink_file_and_non_strict_parents(
    tmp_path: Path,
) -> None:
    source = _source(tmp_path)
    source_path = Mock()
    source_path.is_symlink.return_value = False
    source_path.is_file.return_value = True
    source_path.parent.resolve.return_value = tmp_path / "raw"
    source_root = Mock()
    source_root.resolve.return_value = tmp_path / "raw"
    fake_source = SimpleNamespace(path=source_path)

    build_module._verify_direct_child(fake_source, source_root)

    source_path.parent.resolve.assert_called_once_with(strict=False)
    source_root.resolve.assert_called_once_with(strict=False)
    assert source.path.is_file()


def test_verify_direct_child_reports_symlink_and_outside_paths(tmp_path: Path) -> None:
    source = _source(tmp_path)
    outside = tmp_path / "outside.osm.pbf"
    outside.write_bytes(b"outside")
    paths = Paths(source_root=source.path.parent, data_root=tmp_path / "generated")

    symlink = tmp_path / "link.osm.pbf"
    try:
        symlink.symlink_to(outside)
    except OSError:
        pytest.skip("filesystem does not support symlinks")
    with pytest.raises(
        build_module.PipelineError,
        match=rf"source is not a regular direct child: {symlink}",
    ):
        build_module._verify_direct_child(SimpleNamespace(path=symlink), paths.source_root)

    with pytest.raises(
        build_module.PipelineError,
        match=rf"source is not inside source root: {outside}",
    ):
        build_module._verify_direct_child(SimpleNamespace(path=outside), paths.source_root)


def test_transform_one_forwards_record_and_source_and_counts_rejections() -> None:
    record = ExportRecord(
        geometry_ewkb_hex="0103",
        osm_type="way",
        osm_id=42,
        version=1,
        changeset=1,
        timestamp=None,
        tags={"description": "present"},
    )
    counts = build_module._Counts()
    transformed = {"description": "value"}

    with patch.object(build_module, "transform_record", return_value=transformed) as transform:
        assert build_module._transform_one(record, "region.osm.pbf", counts) is transformed
    transform.assert_called_once_with(record, "region.osm.pbf")
    assert counts.rejections == {}

    with patch.object(
        build_module,
        "transform_record",
        side_effect=RejectedFeature("no_description"),
    ):
        assert build_module._transform_one(record, "region.osm.pbf", counts) is None
        assert build_module._transform_one(record, "region.osm.pbf", counts) is None
    assert counts.rejections == {"no_description": 2}


def test_transform_one_short_circuits_missing_description_before_transform() -> None:
    record = ExportRecord(
        geometry_ewkb_hex="0103",
        osm_type="way",
        osm_id=42,
        version=1,
        changeset=1,
        timestamp=None,
        tags={"name": "without a description"},
    )
    counts = build_module._Counts()

    with patch.object(
        build_module,
        "transform_record",
        side_effect=AssertionError("expected rejection should not transform"),
    ):
        assert build_module._transform_one(record, "region.osm.pbf", counts) is None

    assert counts.rejections == {"no_nonempty_description": 1}


def test_transform_stream_counts_rows_yields_transformed_values_and_reports_progress() -> None:
    records = [object(), object(), object()]
    counts = build_module._Counts()
    callback = Mock()
    transformed = [{"row": 1}, None, {"row": 3}]

    with patch.object(build_module, "_transform_one", side_effect=transformed) as transform:
        output = list(
            build_module._transform_stream(
                records,
                "region.osm.pbf",
                counts,
                progress_callback=callback,
                progress_interval=1,
            )
        )

    assert output == [{"row": 1}, {"row": 3}]
    assert counts.emitted == 3
    assert callback.call_args_list == [call(1, 1), call(2, 1), call(3, 2)]
    assert transform.call_args_list == [
        call(records[0], "region.osm.pbf", counts),
        call(records[1], "region.osm.pbf", counts),
        call(records[2], "region.osm.pbf", counts),
    ]


def test_transform_stream_clamps_zero_interval_and_observes_exact_boundary() -> None:
    records = [object(), object()]
    counts = build_module._Counts()
    callback = Mock()

    with patch.object(build_module, "_transform_one", side_effect=[{}, {}]):
        assert list(
            build_module._transform_stream(
                records,
                "source",
                counts,
                progress_callback=callback,
                progress_interval=0,
            )
        ) == [{}, {}]
    assert callback.call_args_list == [call(1, 1), call(2, 2)]

    no_callback_counts = build_module._Counts()
    with patch.object(build_module, "_transform_one", return_value={}):
        assert list(
            build_module._transform_stream(
                [object()],
                "source",
                no_callback_counts,
                progress_callback=None,
                progress_interval=1,
            )
        ) == [{}]

    boundary_counts = build_module._Counts()
    boundary_callback = Mock()
    with patch.object(build_module, "_transform_one", return_value={}):
        for _ in build_module._transform_stream(
            range(4),
            "source",
            boundary_counts,
            progress_callback=boundary_callback,
            progress_interval=2,
        ):
            pass
    assert boundary_callback.call_args_list == [call(2, 2), call(4, 4)]


def test_transform_stream_keeps_public_default_progress_interval() -> None:
    assert (
        inspect.signature(build_module._transform_stream).parameters["progress_interval"].default
        == 100_000
    )


def test_transform_stream_default_interval_reports_at_100000_emissions() -> None:
    counts = build_module._Counts()
    callback = Mock()
    with patch.object(build_module, "_transform_one", return_value={}):
        for _ in build_module._transform_stream(
            range(100_000), "source", counts, progress_callback=callback
        ):
            pass

    callback.assert_called_once_with(100_000, 100_000)


def test_artifact_paths_creates_nested_output_directories_and_manifest_name(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source = _source(tmp_path)
    paths = Paths(source_root=source.path.parent, data_root=tmp_path / "generated" / "nested")

    original_resolve = Path.resolve
    resolve_calls: list[tuple[Path, dict[str, object]]] = []

    def record_resolve(path: Path, *args: object, **kwargs: object) -> Path:
        resolve_calls.append((path, kwargs))
        return original_resolve(path, *args, **kwargs)

    monkeypatch.setattr(Path, "resolve", record_resolve)
    output, manifest = build_module._artifact_paths(source, paths)

    assert output == paths.data_root / "data" / "region.parquet"
    assert manifest == paths.data_root / "manifests" / "region.manifest.json"
    assert output.parent.is_dir()
    assert manifest.parent.is_dir()
    assert resolve_calls
    assert all(kwargs == {"strict": False} for _path, kwargs in resolve_calls)
    assert build_module._artifact_paths(source, paths) == (output, manifest)


def test_artifact_paths_rejects_artifacts_inside_immutable_source(tmp_path: Path) -> None:
    source_root = tmp_path / "raw"
    source_root.mkdir()
    source = Source(source_root / "region.osm.pbf", "region.osm.pbf", "region.parquet", 1, 1)
    paths = Paths(source_root=source_root, data_root=source_root)

    with pytest.raises(
        build_module.PipelineError,
        match=r"artifact path inside immutable source: .*region\.parquet",
    ):
        build_module._artifact_paths(source, paths)


def test_resumable_manifest_requires_both_files_and_matching_identity(
    tmp_path: Path,
) -> None:
    source = _source(tmp_path)
    output = tmp_path / "output.parquet"
    manifest_path = tmp_path / "manifest.json"
    manifest = SimpleNamespace()
    output.write_bytes(b"output")
    manifest_path.write_bytes(b"manifest")

    with (
        patch.object(build_module, "read_manifest", return_value=manifest) as read_manifest,
        patch.object(build_module, "source_identity_for", return_value="source-id") as source_id,
        patch.object(build_module, "output_identity_for", return_value="output-id") as output_id,
        patch.object(build_module, "is_resumable", return_value=True) as resumable,
    ):
        assert build_module._resumable_manifest(source, output, manifest_path) is manifest

    read_manifest.assert_called_once_with(manifest_path)
    source_id.assert_called_once_with(source.path)
    output_id.assert_called_once_with(output)
    resumable.assert_called_once_with(manifest, "source-id", "output-id")

    manifest_path.unlink()
    assert build_module._resumable_manifest(source, output, manifest_path) is None


@pytest.mark.parametrize(
    ("output_exists", "manifest_exists"),
    [(False, False), (False, True), (True, False)],
)
def test_resumable_manifest_rejects_any_missing_artifact(
    tmp_path: Path,
    output_exists: bool,
    manifest_exists: bool,
) -> None:
    source = _source(tmp_path)
    output = tmp_path / "output.parquet"
    manifest_path = tmp_path / "manifest.json"
    if output_exists:
        output.write_bytes(b"output")
    if manifest_exists:
        manifest_path.write_bytes(b"manifest")

    with patch.object(build_module, "read_manifest") as read_manifest:
        assert build_module._resumable_manifest(source, output, manifest_path) is None
    read_manifest.assert_not_called()


def test_resumable_manifest_returns_none_for_invalid_or_nonresumable_manifests(
    tmp_path: Path,
) -> None:
    source = _source(tmp_path)
    output = tmp_path / "output.parquet"
    manifest_path = tmp_path / "manifest.json"
    output.write_bytes(b"output")
    manifest_path.write_bytes(b"manifest")

    with patch.object(
        build_module,
        "read_manifest",
        side_effect=build_module.ManifestError("invalid"),
    ):
        assert build_module._resumable_manifest(source, output, manifest_path) is None

    with (
        patch.object(build_module, "read_manifest", return_value=object()),
        patch.object(build_module, "is_resumable", return_value=False),
    ):
        assert build_module._resumable_manifest(source, output, manifest_path) is None


def test_reusable_build_result_returns_none_or_complete_skipped_result(tmp_path: Path) -> None:
    source = _source(tmp_path)
    output = tmp_path / "output.parquet"
    manifest_path = tmp_path / "manifest.json"
    counts = SimpleNamespace(emitted_features=4, included_rows=3, rejections={"reason": 1})
    manifest = SimpleNamespace(counts=counts)

    with (
        patch.object(build_module, "_resumable_manifest", return_value=None),
        patch.object(build_module, "_valid_output", return_value=True),
    ):
        assert build_module._reusable_build_result(source, output, manifest_path) is None

    with (
        patch.object(build_module, "_resumable_manifest", return_value=manifest),
        patch.object(build_module, "_valid_output", return_value=True),
    ):
        result = build_module._reusable_build_result(source, output, manifest_path)

    assert result == build_module.BuildResult(
        source_name=source.name,
        output_name=source.output_name,
        status="skipped",
        emitted_features=4,
        included_rows=3,
        rejections={"reason": 1},
        output_path=output,
        manifest_path=manifest_path,
    )


def test_valid_output_returns_true_or_false_for_storage_validation(
    tmp_path: Path,
) -> None:
    output = tmp_path / "output.parquet"
    with patch.object(build_module, "validate_geoparquet") as validate:
        assert build_module._valid_output(output) is True
    validate.assert_called_once_with(output)

    with patch.object(
        build_module,
        "validate_geoparquet",
        side_effect=build_module.StorageError("invalid parquet"),
    ):
        assert build_module._valid_output(output) is False


def test_build_fresh_forwards_export_writer_progress_and_manifest_metadata(
    tmp_path: Path,
) -> None:
    source = _source(tmp_path)
    output = tmp_path / "data" / "region.parquet"
    manifest_path = tmp_path / "manifests" / "region.manifest.json"
    config = tmp_path / "export.json"
    clock = Mock(side_effect=["started", "completed"])
    exporter = Mock(return_value=object())
    writer = Mock(return_value=3)
    progress_callback = Mock()
    transformed = object()

    def fake_transform_stream(
        _records: object,
        _source_name: str,
        counts: build_module._Counts,
        **_kwargs: object,
    ) -> object:
        counts.emitted = 2
        counts.rejections = {"reason": 1}
        return transformed

    with (
        patch.object(
            build_module, "_transform_stream", side_effect=fake_transform_stream
        ) as transform_stream,
        patch.object(build_module, "safe_osmium_version", return_value="osmium-version"),
        patch.object(build_module, "current_area_policy_sha256", return_value="area-hash"),
        patch.object(build_module, "current_output_algorithm_revision", return_value="output-rev"),
        patch.object(build_module, "source_identity_for", return_value="source-id"),
        patch.object(build_module, "output_identity_for", return_value="output-id"),
        patch.object(build_module, "current_dependency_versions", return_value={"x": "1"}),
        patch.object(build_module, "current_code_revision", return_value="code-rev"),
        patch.object(build_module, "write_manifest") as write_manifest,
    ):
        result = build_module._build_fresh(
            source,
            output,
            manifest_path,
            exporter=exporter,
            writer=writer,
            executable="osmium-custom",
            export_config=config,
            clock=clock,
            batch_size=17,
            progress_interval=23,
            progress_callback=progress_callback,
        )

    exporter.assert_called_once_with(source.path, config)
    transform_stream.assert_called_once_with(
        exporter.return_value,
        source.name,
        transform_stream.call_args.args[2],
        progress_callback=progress_callback,
        progress_interval=23,
    )
    writer.assert_called_once_with(transformed, output, batch_size=17)
    assert clock.call_args_list == [call(), call()]
    manifest = write_manifest.call_args.args[0]
    assert manifest.started_at == "started"
    assert manifest.completed_at == "completed"
    assert manifest.code_revision == "code-rev"
    assert manifest.counts.emitted_features == 2
    assert manifest.counts.included_rows == 3
    assert manifest.counts.rejections == {"reason": 1}
    assert write_manifest.call_args.args[1] == manifest_path
    assert result == build_module.BuildResult(
        source_name=source.name,
        output_name=source.output_name,
        status="built",
        emitted_features=2,
        included_rows=3,
        rejections={"reason": 1},
        output_path=output,
        manifest_path=manifest_path,
    )


def test_build_one_forwards_defaults_and_injected_dependencies(tmp_path: Path) -> None:
    source = _source(tmp_path)
    paths = Paths(source_root=source.path.parent, data_root=tmp_path / "generated")
    config = tmp_path / "export.json"
    exporter = Mock()
    writer = Mock()
    clock = Mock()
    callback = Mock()
    fresh_result = object()
    output = tmp_path / "output.parquet"
    manifest = tmp_path / "manifest.json"

    with (
        patch.object(Paths, "validate") as validate,
        patch.object(build_module, "_verify_direct_child") as verify,
        patch.object(build_module, "_artifact_paths", return_value=(output, manifest)) as artifacts,
        patch.object(build_module, "_reusable_build_result", return_value=None) as reusable,
        patch.object(build_module, "_build_fresh", return_value=fresh_result) as fresh,
    ):
        result = build_module.build_one(
            source,
            paths,
            export_config=config,
            executable="custom-osmium",
            exporter=exporter,
            writer=writer,
            clock=clock,
            batch_size=17,
            progress_interval=23,
            progress_callback=callback,
        )

    assert result is fresh_result
    validate.assert_called_once_with()
    verify.assert_called_once_with(source, paths.source_root)
    artifacts.assert_called_once_with(source, paths)
    reusable.assert_called_once_with(source, output, manifest)
    fresh.assert_called_once_with(
        source,
        output,
        manifest,
        exporter=exporter,
        writer=writer,
        executable="custom-osmium",
        export_config=config,
        clock=clock,
        batch_size=17,
        progress_interval=23,
        progress_callback=callback,
    )


def test_build_one_default_exporter_forwards_executable(tmp_path: Path) -> None:
    source = _source(tmp_path)
    paths = Paths(source_root=source.path.parent, data_root=tmp_path / "generated")
    captured: dict[str, object] = {}

    def capture_fresh(*args: object, **kwargs: object) -> object:
        captured.update(kwargs)
        return "fresh"

    with (
        patch.object(build_module, "_artifact_paths", return_value=(Path("out"), Path("manifest"))),
        patch.object(build_module, "_reusable_build_result", return_value=None),
        patch.object(build_module, "_build_fresh", side_effect=capture_fresh),
        patch.object(build_module, "_verify_direct_child"),
    ):
        assert (
            build_module.build_one(
                source,
                paths,
                export_config=Path("config"),
                executable="custom",
            )
            == "fresh"
        )

    exporter = captured["exporter"]
    with patch.object(build_module, "stream_export", return_value=[]) as stream:
        assert list(exporter(source.path, Path("config"))) == []  # type: ignore[operator]
    stream.assert_called_once_with(source.path, Path("config"), executable="custom")


def test_build_one_omitted_defaults_reach_fresh_builder(tmp_path: Path) -> None:
    source = _source(tmp_path)
    paths = Paths(source_root=source.path.parent, data_root=tmp_path / "generated")
    exporter = Mock()
    writer = Mock()
    clock = Mock()
    captured: dict[str, object] = {}

    def capture_fresh(*args: object, **kwargs: object) -> str:
        captured.update(kwargs)
        return "fresh"

    with (
        patch.object(build_module, "_artifact_paths", return_value=(Path("out"), Path("manifest"))),
        patch.object(build_module, "_reusable_build_result", return_value=None),
        patch.object(build_module, "_build_fresh", side_effect=capture_fresh),
        patch.object(build_module, "_verify_direct_child"),
    ):
        assert (
            build_module.build_one(
                source,
                paths,
                export_config=Path("config"),
                exporter=exporter,
                writer=writer,
                clock=clock,
            )
            == "fresh"
        )

    assert captured["executable"] == "osmium"
    assert captured["batch_size"] == 1024
    assert captured["progress_interval"] == 100_000
    assert captured["progress_callback"] is None


def test_build_one_public_default_parameters_are_stable() -> None:
    parameters = inspect.signature(build_module.build_one).parameters
    assert parameters["executable"].default == "osmium"
    assert parameters["batch_size"].default == 1024
    assert parameters["progress_interval"].default == 100_000
    assert parameters["progress_callback"].default is None
