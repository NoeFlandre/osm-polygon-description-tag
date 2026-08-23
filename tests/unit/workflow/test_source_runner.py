"""Direct contracts for per-source workflow decisions and build forwarding."""

from pathlib import Path
from types import SimpleNamespace

import pytest

import osm_polygon_description_tag.workflow.source_runner as source_runner
from osm_polygon_description_tag.osm.discovery import Source
from osm_polygon_description_tag.publication.state import PublicationStateError
from osm_polygon_description_tag.runtime.config import Paths
from osm_polygon_description_tag.workflow.build import BuildResult
from osm_polygon_description_tag.workflow.source_runner import (
    STATUS_BUILT,
    STATUS_PUBLISHED,
    STATUS_REUSED,
    SourceOutcome,
    _build_source,
    _manifest_outcome,
    cast_dict,
    local_artifact_is_complete,
    process_one,
    published_state_matches,
    read_publication_state,
)


class _Logger:
    def __init__(self) -> None:
        self.events: list[tuple[str, dict[str, object]]] = []

    def event(self, name: str, **fields: object) -> None:
        self.events.append((name, fields))


def _source_and_paths(tmp_path: Path) -> tuple[Source, Paths]:
    source_root = tmp_path / "raw"
    data_root = tmp_path / "generated"
    source_root.mkdir()
    data_root.mkdir()
    source_path = source_root / "a.osm.pbf"
    source_path.write_bytes(b"source")
    source = Source(source_path, "a.osm.pbf", "a.parquet", 6, source_path.stat().st_mtime_ns)
    return source, Paths(source_root=source_root, data_root=data_root)


def test_build_source_forwards_clock_and_all_build_options(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    source, paths = _source_and_paths(tmp_path)
    output = paths.data_root / "data" / source.output_name
    output.parent.mkdir(parents=True)
    output.write_bytes(b"built")
    seen: dict[str, object] = {}
    logger = _Logger()
    export_config = object()
    monkeypatch.setattr(source_runner, "osmium_export_config", lambda: export_config)

    def clock() -> str:
        return "2026-01-01T00:00:00+00:00"

    exporter = object()

    def fake_build_one(actual_source: Source, actual_paths: Paths, **kwargs: object) -> BuildResult:
        seen["source"] = actual_source
        seen["paths"] = actual_paths
        seen.update(kwargs)
        kwargs["progress_callback"](8, 6)  # type: ignore[operator]
        return BuildResult(
            source_name=actual_source.name,
            output_name=actual_source.output_name,
            status="built",
            emitted_features=4,
            included_rows=3,
            rejections={"rejected": 1},
            output_path=output,
            manifest_path=paths.data_root / "manifests" / "a.manifest.json",
        )

    monkeypatch.setattr(source_runner, "build_one", fake_build_one)

    outcome = _build_source(
        source,
        paths,
        clock=clock,
        exporter=exporter,  # type: ignore[arg-type]
        progress_interval=7,
        logger=logger,  # type: ignore[arg-type]
        source_index=2,
        source_total=5,
        osmium_executable="custom-osmium",
    )

    assert seen["source"] is source
    assert seen["paths"] is paths
    assert seen["clock"] is clock
    assert seen["exporter"] is exporter
    assert seen["export_config"] is export_config
    assert seen["progress_interval"] == 7
    assert seen["executable"] == "custom-osmium"
    assert callable(seen["progress_callback"])
    assert logger.events == [
        (
            "source_decision",
            {
                "level": "INFO",
                "source": "a.osm.pbf",
                "source_index": 2,
                "source_total": 5,
                "decision": "build",
            },
        ),
        (
            "build_start",
            {
                "level": "INFO",
                "source": "a.osm.pbf",
                "source_index": 2,
                "source_total": 5,
            },
        ),
        (
            "build_progress",
            {
                "level": "INFO",
                "source": "a.osm.pbf",
                "source_index": 2,
                "source_total": 5,
                "emitted": 8,
                "included": 6,
            },
        ),
        (
            "build_complete",
            {
                "level": "INFO",
                "source": "a.osm.pbf",
                "source_index": 2,
                "source_total": 5,
                "rows": 3,
                "bytes": 5,
            },
        ),
    ]
    assert outcome == SourceOutcome(
        source_name="a.osm.pbf",
        status=STATUS_BUILT,
        included_rows=3,
        output_bytes=5,
        note="freshly built; upload required",
    )


@pytest.mark.parametrize("published", [True, False])
def test_process_one_reuses_complete_artifact_as_published_or_upload_required(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, published: bool
) -> None:
    source, paths = _source_and_paths(tmp_path)
    output = paths.data_root / "data" / source.output_name
    output.parent.mkdir(parents=True)
    output.write_bytes(b"local")
    manifest = object()
    monkeypatch.setattr(
        source_runner, "local_artifact_is_complete", lambda *_args: (True, manifest)
    )
    monkeypatch.setattr(source_runner, "_published_entry", lambda *_args: {"published": published})
    monkeypatch.setattr(
        source_runner,
        "published_state_matches",
        lambda *_args: published,
    )
    monkeypatch.setattr(
        source_runner,
        "_manifest_outcome",
        lambda actual_source, _manifest, actual_output, status, note: SourceOutcome(
            actual_source.name, status, output_bytes=actual_output.stat().st_size, note=note
        ),
    )
    monkeypatch.setattr(
        source_runner,
        "_build_source",
        lambda *_args, **_kwargs: pytest.fail("complete artifacts must not be rebuilt"),
    )

    outcome = process_one(source, paths, clock=lambda: "now")

    assert outcome.status == (STATUS_PUBLISHED if published else STATUS_REUSED)


def test_process_one_builds_when_local_artifact_is_incomplete(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    source, paths = _source_and_paths(tmp_path)
    expected = SourceOutcome(source.name, STATUS_BUILT)
    monkeypatch.setattr(source_runner, "local_artifact_is_complete", lambda *_args: (False, None))
    monkeypatch.setattr(source_runner, "_published_entry", lambda *_args: {})
    monkeypatch.setattr(source_runner, "_build_source", lambda *_args, **_kwargs: expected)

    assert process_one(source, paths, clock=lambda: "now") is expected


def test_manifest_outcome_contains_manifest_counts_file_size_and_note(
    tmp_path: Path,
) -> None:
    source, paths = _source_and_paths(tmp_path)
    output = paths.data_root / "data" / source.output_name
    output.parent.mkdir(parents=True)
    output.write_bytes(b"artifact")
    manifest = SimpleNamespace(counts=SimpleNamespace(included_rows=17))

    outcome = _manifest_outcome(source, manifest, output, STATUS_REUSED, "reuse note")

    assert outcome == SourceOutcome(
        source_name=source.name,
        status=STATUS_REUSED,
        included_rows=17,
        output_bytes=8,
        remote_revision=None,
        note="reuse note",
    )


def test_build_source_without_logger_passes_no_progress_callback(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    source, paths = _source_and_paths(tmp_path)
    output = paths.data_root / "data" / source.output_name
    output.parent.mkdir(parents=True)
    output.write_bytes(b"built")
    seen: dict[str, object] = {}

    def fake_build_one(_source: Source, _paths: Paths, **kwargs: object) -> BuildResult:
        seen.update(kwargs)
        return BuildResult(
            source_name=source.name,
            output_name=source.output_name,
            status="built",
            emitted_features=1,
            included_rows=1,
            rejections={},
            output_path=output,
            manifest_path=paths.data_root / "manifests" / "a.manifest.json",
        )

    monkeypatch.setattr(source_runner, "build_one", fake_build_one)

    _build_source(
        source,
        paths,
        clock=lambda: "now",
        exporter=None,
        progress_interval=100,
        logger=None,
        source_index=0,
        source_total=1,
        osmium_executable="osmium",
    )

    assert seen["progress_callback"] is None


def _write_artifact_pair(paths: Paths, source: Source) -> tuple[Path, Path]:
    output = paths.data_root / "data" / source.output_name
    manifest_path = (
        paths.data_root
        / "manifests"
        / f"{source.output_name.removesuffix('.parquet')}.manifest.json"
    )
    output.parent.mkdir(parents=True)
    manifest_path.parent.mkdir(parents=True)
    output.write_bytes(b"artifact")
    manifest_path.write_text("{}", encoding="utf-8")
    return output, manifest_path


def test_local_artifact_is_complete_requires_both_expected_files(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    source, paths = _source_and_paths(tmp_path)
    output, manifest_path = _write_artifact_pair(paths, source)
    monkeypatch.setattr(source_runner, "validate_geoparquet", lambda _path: None)
    monkeypatch.setattr(source_runner, "read_manifest", lambda _path: object())
    monkeypatch.setattr(source_runner, "is_resumable", lambda *_args: True)

    output.unlink()
    assert local_artifact_is_complete(paths, source) == (False, None)
    output.write_bytes(b"artifact")
    manifest_path.unlink()
    assert local_artifact_is_complete(paths, source) == (False, None)


def test_local_artifact_is_complete_returns_false_for_validation_or_identity_failure(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    source, paths = _source_and_paths(tmp_path)
    _write_artifact_pair(paths, source)
    manifest = object()
    monkeypatch.setattr(source_runner, "read_manifest", lambda _path: manifest)
    monkeypatch.setattr(source_runner, "is_resumable", lambda *_args: False)
    monkeypatch.setattr(source_runner, "validate_geoparquet", lambda _path: None)
    assert local_artifact_is_complete(paths, source) == (False, None)

    def fail_validation(_path: Path) -> None:
        raise source_runner.StorageError("invalid parquet")

    monkeypatch.setattr(source_runner, "validate_geoparquet", fail_validation)
    assert local_artifact_is_complete(paths, source) == (False, None)


def test_local_artifact_is_complete_propagates_unexpected_validation_errors(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    source, paths = _source_and_paths(tmp_path)
    _write_artifact_pair(paths, source)

    def fail_unexpectedly(_path: Path) -> None:
        raise RuntimeError("unexpected validator failure")

    monkeypatch.setattr(source_runner, "validate_geoparquet", fail_unexpectedly)

    with pytest.raises(RuntimeError, match="unexpected validator failure"):
        local_artifact_is_complete(paths, source)


def test_local_artifact_is_complete_returns_manifest_for_resumable_pair(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    source, paths = _source_and_paths(tmp_path)
    _write_artifact_pair(paths, source)
    manifest = object()
    expected_output = paths.data_root / "data" / source.output_name
    expected_manifest = (
        paths.data_root
        / "manifests"
        / f"{source.output_name.removesuffix('.parquet')}.manifest.json"
    )

    def validate(path: Path) -> None:
        assert path == expected_output

    def read(path: Path) -> object:
        assert path == expected_manifest
        return manifest

    monkeypatch.setattr(source_runner, "validate_geoparquet", validate)
    monkeypatch.setattr(source_runner, "read_manifest", read)
    monkeypatch.setattr(source_runner, "is_resumable", lambda *_args: True)

    assert local_artifact_is_complete(paths, source) == (True, manifest)


def test_published_state_matches_requires_all_four_identities(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    source, paths = _source_and_paths(tmp_path)
    output = paths.data_root / "data" / source.output_name
    source_identity = SimpleNamespace(sha256="source-sha")
    output_identity = SimpleNamespace(sha256="output-sha")
    manifest = SimpleNamespace(source=source_identity, output=output_identity)
    monkeypatch.setattr(source_runner, "source_identity_for", lambda _path: source_identity)
    monkeypatch.setattr(source_runner, "output_identity_for", lambda _path: output_identity)
    existing = {"source_sha256": "source-sha", "output_sha256": "output-sha"}

    assert published_state_matches(existing, manifest, source, output) is True
    assert (
        published_state_matches({**existing, "source_sha256": "wrong"}, manifest, source, output)
        is False
    )
    assert (
        published_state_matches({**existing, "output_sha256": "wrong"}, manifest, source, output)
        is False
    )
    assert (
        published_state_matches(
            existing, SimpleNamespace(source=object(), output=output_identity), source, output
        )
        is False
    )
    assert (
        published_state_matches(
            existing, SimpleNamespace(source=source_identity, output=object()), source, output
        )
        is False
    )


def test_published_entry_defaults_missing_state_sections_to_empty(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(source_runner, "read_publication_state", lambda *_args: {})
    assert source_runner._published_entry(Path("data"), "missing.osm.pbf") == {}

    monkeypatch.setattr(
        source_runner,
        "read_publication_state",
        lambda *_args: {"published": {"a.osm.pbf": {"remote_revision": "r1"}}},
    )
    assert source_runner._published_entry(Path("data"), "a.osm.pbf") == {"remote_revision": "r1"}


def test_state_wrappers_translate_publication_errors(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def fail(*_args: object, **_kwargs: object) -> object:
        raise PublicationStateError("malformed state")

    monkeypatch.setattr(source_runner, "_state_read_publication_state", fail)
    with pytest.raises(source_runner.OrchestratorError, match="^malformed state$"):
        read_publication_state(Path("data"))

    monkeypatch.setattr(source_runner, "_state_cast_dict", fail)
    with pytest.raises(source_runner.OrchestratorError, match="^malformed state$"):
        cast_dict(object())


def test_process_one_forwards_all_build_options_and_defaults(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    source, paths = _source_and_paths(tmp_path)
    captured: dict[str, object] = {}
    monkeypatch.setattr(source_runner, "local_artifact_is_complete", lambda *_args: (False, None))
    monkeypatch.setattr(source_runner, "_published_entry", lambda *_args: {})

    def fake_build(actual_source: Source, actual_paths: Paths, **kwargs: object) -> SourceOutcome:
        captured["source"] = actual_source
        captured["paths"] = actual_paths
        captured.update(kwargs)
        return SourceOutcome(source.name, STATUS_BUILT)

    monkeypatch.setattr(source_runner, "_build_source", fake_build)

    def clock() -> str:
        return "now"

    exporter = object()
    logger = _Logger()

    result = process_one(
        source,
        paths,
        clock=clock,
        exporter=exporter,  # type: ignore[arg-type]
        progress_interval=7,
        logger=logger,  # type: ignore[arg-type]
        source_index=2,
        source_total=5,
        osmium_executable="custom-osmium",
    )

    assert result.status == STATUS_BUILT
    assert captured == {
        "source": source,
        "paths": paths,
        "clock": clock,
        "exporter": exporter,
        "progress_interval": 7,
        "logger": logger,
        "source_index": 2,
        "source_total": 5,
        "osmium_executable": "custom-osmium",
    }

    captured.clear()
    process_one(source, paths, clock=clock)
    assert captured["progress_interval"] == 100_000
    assert captured["logger"] is None
    assert captured["source_index"] == 0
    assert captured["source_total"] == 0
    assert captured["osmium_executable"] == "osmium"


@pytest.mark.parametrize("published", [True, False])
def test_process_one_logs_and_returns_exact_complete_artifact_decision(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    published: bool,
) -> None:
    source, paths = _source_and_paths(tmp_path)
    output = paths.data_root / "data" / source.output_name
    output.parent.mkdir(parents=True)
    output.write_bytes(b"artifact")
    manifest = SimpleNamespace(counts=SimpleNamespace(included_rows=4))
    logger = _Logger()
    monkeypatch.setattr(
        source_runner, "local_artifact_is_complete", lambda *_args: (True, manifest)
    )
    monkeypatch.setattr(source_runner, "_published_entry", lambda *_args: {})

    def matches(
        _existing: dict[str, object],
        _manifest: object,
        _source: Source,
        actual_output: Path,
    ) -> bool:
        assert actual_output == output
        return published

    monkeypatch.setattr(source_runner, "published_state_matches", matches)
    monkeypatch.setattr(
        source_runner,
        "_build_source",
        lambda *_args, **_kwargs: pytest.fail("complete artifact must not be built"),
    )

    outcome = process_one(
        source,
        paths,
        clock=lambda: "now",
        logger=logger,  # type: ignore[arg-type]
        source_index=2,
        source_total=5,
    )

    expected_status = STATUS_PUBLISHED if published else STATUS_REUSED
    expected_note = (
        "already published; nothing to do"
        if published
        else "local artifact reused; upload required"
    )
    expected_decision = "already-published" if published else "reuse-local"
    assert outcome == SourceOutcome(
        source_name=source.name,
        status=expected_status,
        included_rows=4,
        output_bytes=8,
        remote_revision=None,
        note=expected_note,
    )
    assert logger.events == [
        (
            "source_decision",
            {
                "level": "INFO",
                "source": source.name,
                "source_index": 2,
                "source_total": 5,
                "decision": expected_decision,
            },
        )
    ]


def test_process_one_builds_when_manifest_is_missing_even_if_artifact_flag_is_true(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    source, paths = _source_and_paths(tmp_path)
    expected = SourceOutcome(source.name, STATUS_BUILT)
    monkeypatch.setattr(source_runner, "local_artifact_is_complete", lambda *_args: (True, None))
    monkeypatch.setattr(source_runner, "_published_entry", lambda *_args: {})
    monkeypatch.setattr(source_runner, "_build_source", lambda *_args, **_kwargs: expected)

    assert process_one(source, paths, clock=lambda: "now") is expected
