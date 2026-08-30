"""Focused contracts for workflow finalization boundaries."""

import hashlib
import subprocess
from io import StringIO
from pathlib import Path
from types import SimpleNamespace

import pytest

import osm_polygon_description_tag.workflow.finalization as finalization_module
from osm_polygon_description_tag.dataset.storage import StorageError
from osm_polygon_description_tag.osm.discovery import Source
from osm_polygon_description_tag.publication.models import PublicationError, UploadItem, UploadPlan
from osm_polygon_description_tag.runtime.config import Paths
from osm_polygon_description_tag.runtime.logging import RunLogger
from osm_polygon_description_tag.workflow.finalization import (
    AREA_HISTOGRAM_ASSET_RELATIVE,
    DATASET_CARD_HERO_ASSET_RELATIVE,
    H3_MAP_ASSET_RELATIVE,
    OrchestratorError,
    _append_if_present,
    _call_metadata_verifier,
    _cast_dict,
    _completeness_sets,
    _inspect_artifact,
    _log_metadata_start,
    _log_metadata_state,
    _metadata_retry_observer,
    _metadata_skip_revision,
    _metadata_state_matches,
    _persist_metadata_state,
    _read_publication_state,
    _run_metadata_upload,
    _upload_metadata,
    _verify_metadata,
    _write_metadata_state,
    refresh_dataset_docs,
    upload_final_metadata,
    verify_final_completeness,
)


def _paths(tmp_path: Path) -> Paths:
    source_root = tmp_path / "raw"
    data_root = tmp_path / "generated"
    source_root.mkdir()
    data_root.mkdir()
    return Paths(source_root=source_root, data_root=data_root)


def _logger(tmp_path: Path) -> RunLogger:
    return RunLogger(
        data_root=tmp_path / "logs-root",
        run_id="test-run",
        clock=lambda: "2026-01-01T00:00:00+00:00",
        stderr=StringIO(),
    )


def test_refresh_docs_skips_when_no_parquet_exists(tmp_path: Path) -> None:
    paths = _paths(tmp_path)
    logger = _logger(tmp_path)
    refresh_dataset_docs(paths, clock=lambda: "now", logger=logger)
    logger.close()


def test_refresh_docs_emits_event_after_generation(tmp_path: Path) -> None:
    paths = _paths(tmp_path)
    (paths.data_root / "data").mkdir()
    (paths.data_root / "data" / "a.parquet").write_bytes(b"placeholder")
    logger = _logger(tmp_path)
    calls: list[Path] = []

    def generator(data_root: Path, _template: Path, *, clock: object) -> None:
        calls.append(data_root)

    refresh_dataset_docs(paths, clock=lambda: "now", logger=logger, docs_generator=generator)
    logger.close()
    assert calls == [paths.data_root]


def test_refresh_docs_wraps_generation_errors(tmp_path: Path) -> None:
    paths = _paths(tmp_path)
    (paths.data_root / "data").mkdir()
    (paths.data_root / "data" / "a.parquet").write_bytes(b"placeholder")
    logger = _logger(tmp_path)

    def generator(*_args: object, **_kwargs: object) -> None:
        raise RuntimeError("broken docs")

    with pytest.raises(OrchestratorError, match="dataset card refresh failed"):
        refresh_dataset_docs(paths, clock=lambda: "now", logger=logger, docs_generator=generator)
    logger.close()


def test_upload_metadata_skips_without_local_dataset(tmp_path: Path) -> None:
    paths = _paths(tmp_path)
    assert (
        upload_final_metadata(
            paths,
            verifier=None,
            upload_runner=None,
            upload_timeout=None,
            clock=lambda: "now",
        )
        is None
    )


def test_upload_metadata_requires_a_real_data_directory_before_globbing(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    paths = _paths(tmp_path)
    data_dir = paths.data_root / "data"
    glob_calls: list[str] = []

    real_is_dir = Path.is_dir
    real_glob = Path.glob

    def is_dir(path: Path) -> bool:
        if path == data_dir:
            return False
        return real_is_dir(path)

    def glob(path: Path, pattern: str):
        if path == data_dir:
            glob_calls.append(pattern)
            return iter([data_dir / "stale.parquet"])
        return real_glob(path, pattern)

    monkeypatch.setattr(Path, "is_dir", is_dir)
    monkeypatch.setattr(Path, "glob", glob)
    monkeypatch.setattr(
        finalization_module,
        "_build_metadata_only_upload_plan",
        lambda _root: pytest.fail("missing data directory must stop before planning"),
    )

    assert (
        upload_final_metadata(
            paths,
            verifier=None,
            upload_runner=None,
            upload_timeout=None,
            clock=lambda: "now",
        )
        is None
    )
    assert glob_calls == []


def test_state_wrappers_translate_malformed_publication_state(tmp_path: Path) -> None:
    state_path = tmp_path / "publication-state.json"
    state_path.write_text("[]", encoding="utf-8")
    with pytest.raises(OrchestratorError, match=r"^expected dict, got list$"):
        _read_publication_state(tmp_path)
    with pytest.raises(OrchestratorError, match=r"^expected dict, got str$"):
        _cast_dict("not-a-dict")


def test_metadata_state_wrapper_forwards_plan_and_translates_errors(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    plan = _metadata_plan(tmp_path)
    calls: list[tuple[Path, UploadPlan]] = []

    def matches(root: Path, actual_plan: UploadPlan) -> bool:
        calls.append((root, actual_plan))
        return True

    monkeypatch.setattr(finalization_module, "_state_metadata_state_matches", matches)
    assert _metadata_state_matches(tmp_path, plan) is True
    assert calls == [(tmp_path, plan)]

    def fail(_root: Path, _plan: UploadPlan) -> bool:
        raise finalization_module.PublicationStateError("malformed metadata state")

    monkeypatch.setattr(finalization_module, "_state_metadata_state_matches", fail)
    with pytest.raises(OrchestratorError, match=r"^malformed metadata state$"):
        _metadata_state_matches(tmp_path, plan)


def test_write_metadata_state_wrapper_forwards_all_fields(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    captured: dict[str, object] = {}

    def write(root: Path, **kwargs: object) -> dict[str, object]:
        captured["root"] = root
        captured.update(kwargs)
        return {"written": True}

    monkeypatch.setattr(finalization_module, "_state_write_metadata_state", write)
    result = _write_metadata_state(
        tmp_path,
        identity_sha256="identity",
        readme_sha256="readme",
        stats_sha256="stats",
        readme_size_bytes=1,
        stats_size_bytes=2,
        h3_map_sha256="h3",
        h3_map_size_bytes=3,
        area_histogram_sha256="histogram",
        area_histogram_size_bytes=4,
        dataset_card_hero_sha256="hero",
        dataset_card_hero_size_bytes=5,
        verified_revision="revision",
        completed_at="now",
    )

    assert result == {"written": True}
    assert captured == {
        "root": tmp_path,
        "identity_sha256": "identity",
        "readme_sha256": "readme",
        "stats_sha256": "stats",
        "readme_size_bytes": 1,
        "stats_size_bytes": 2,
        "h3_map_sha256": "h3",
        "h3_map_size_bytes": 3,
        "area_histogram_sha256": "histogram",
        "area_histogram_size_bytes": 4,
        "dataset_card_hero_sha256": "hero",
        "dataset_card_hero_size_bytes": 5,
        "verified_revision": "revision",
        "completed_at": "now",
    }


def test_metadata_state_wrapper_translates_unsupported_schema(tmp_path: Path) -> None:
    (tmp_path / "publication-state.json").write_text('{"schema_version": 999}', encoding="utf-8")
    with pytest.raises(OrchestratorError, match="unsupported publication state schema"):
        _write_metadata_state(
            tmp_path,
            identity_sha256="id",
            readme_sha256="readme",
            stats_sha256="stats",
            readme_size_bytes=1,
            stats_size_bytes=1,
            verified_revision="rev",
            completed_at="now",
        )


def _metadata_plan(tmp_path: Path) -> UploadPlan:
    return UploadPlan(
        repo_id="NoeFlandre/osm-polygon-description-tag",
        data_root=str(tmp_path),
        files=(UploadItem("README.md", 1, "readme"),),
        identity_sha256="plan-id",
    )


class _EventLogger:
    def __init__(self) -> None:
        self.events: list[tuple[str, dict[str, object]]] = []

    def event(self, name: str, **fields: object) -> None:
        self.events.append((name, fields))


def _source(tmp_path: Path, name: str = "region.osm.pbf") -> Source:
    path = tmp_path / name
    path.write_bytes(b"source")
    return Source(
        path=path,
        name=name,
        output_name=name.removesuffix(".osm.pbf") + ".parquet",
        size_bytes=path.stat().st_size,
        mtime_ns=path.stat().st_mtime_ns,
    )


def test_refresh_docs_forwards_template_clock_and_event_paths(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    paths = _paths(tmp_path)
    data_dir = paths.data_root / "data"
    data_dir.mkdir()
    (data_dir / "region.parquet").write_bytes(b"placeholder")
    template = tmp_path / "template.md"

    def clock() -> str:
        return "now"

    logger = _EventLogger()
    generator_calls: list[tuple[Path, Path, object]] = []

    monkeypatch.setattr(finalization_module, "dataset_card_template", lambda: template)
    real_is_dir = Path.is_dir

    def is_dir(path: Path) -> bool:
        if path.parent == paths.data_root and path.name in {"data", "DATA"}:
            assert path.name == "data"
        return real_is_dir(path)

    monkeypatch.setattr(Path, "is_dir", is_dir)

    def generator(data_root: Path, actual_template: Path, *, clock: object) -> None:
        generator_calls.append((data_root, actual_template, clock))

    refresh_dataset_docs(paths, clock=clock, logger=logger, docs_generator=generator)

    assert generator_calls == [(paths.data_root, template, clock)]
    assert logger.events == [
        (
            "dataset_docs_refreshed",
            {
                "level": "INFO",
                "readme": paths.data_root / "README.md",
                "stats": paths.data_root / "stats.json",
                "assets": paths.data_root / "assets" / "description_polygon_density.png",
            },
        )
    ]


def test_refresh_docs_returns_when_data_directory_has_no_parquet(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    paths = _paths(tmp_path)
    (paths.data_root / "data").mkdir()
    monkeypatch.setattr(
        finalization_module,
        "dataset_card_template",
        lambda: pytest.fail("template must not be loaded"),
    )

    refresh_dataset_docs(paths, clock=lambda: "now", logger=_EventLogger())


def test_upload_final_metadata_forwards_every_stage_argument(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    paths = _paths(tmp_path)
    data_dir = paths.data_root / "data"
    data_dir.mkdir()
    (data_dir / "region.parquet").write_bytes(b"parquet")
    plan = _metadata_plan(paths.data_root)
    logger = _EventLogger()
    verifier = object()
    upload_runner = object()

    def clock() -> str:
        return "now"

    calls: list[tuple[object, ...]] = []
    real_is_dir = Path.is_dir

    def is_dir(path: Path) -> bool:
        if path.parent == paths.data_root and path.name in {"data", "DATA"}:
            assert path.name == "data"
        return real_is_dir(path)

    monkeypatch.setattr(Path, "is_dir", is_dir)

    monkeypatch.setattr(
        finalization_module,
        "_build_metadata_only_upload_plan",
        lambda root: (calls.append(("plan", root)) or plan),
    )

    def skip(root: Path, actual_plan: UploadPlan, actual_logger: object) -> str | None:
        calls.append(("skip", root, actual_plan, actual_logger))
        return None

    monkeypatch.setattr(finalization_module, "_metadata_skip_revision", skip)

    def upload(
        actual_plan: UploadPlan,
        actual_paths: Paths,
        actual_runner: object,
        actual_timeout: float,
        actual_logger: object,
    ) -> None:
        calls.append(
            (
                "upload",
                actual_plan,
                actual_paths,
                actual_runner,
                actual_timeout,
                actual_logger,
            )
        )

    monkeypatch.setattr(finalization_module, "_upload_metadata", upload)
    monkeypatch.setattr(
        finalization_module,
        "_verify_metadata",
        lambda actual_plan, actual_verifier, actual_logger: (
            calls.append(("verify", actual_plan, actual_verifier, actual_logger)) or "verified"
        ),
    )
    monkeypatch.setattr(
        finalization_module,
        "_persist_metadata_state",
        lambda root, actual_plan, revision, actual_clock: calls.append(
            ("persist", root, actual_plan, revision, actual_clock)
        ),
    )
    monkeypatch.setattr(
        finalization_module,
        "_log_metadata_start",
        lambda actual_logger: calls.append(("start", actual_logger)),
    )
    monkeypatch.setattr(
        finalization_module,
        "_log_metadata_state",
        lambda actual_logger, revision: calls.append(("state", actual_logger, revision)),
    )

    def validator(root: Path) -> None:
        calls.append(("validate", root))

    assert (
        upload_final_metadata(
            paths,
            verifier=verifier,  # type: ignore[arg-type]
            upload_runner=upload_runner,  # type: ignore[arg-type]
            upload_timeout=12.5,
            clock=clock,
            logger=logger,
            plan_validator=validator,
        )
        == "verified"
    )
    assert calls == [
        ("plan", paths.data_root),
        ("validate", paths.data_root),
        ("skip", paths.data_root, plan, logger),
        ("start", logger),
        ("upload", plan, paths, upload_runner, 12.5, logger),
        ("verify", plan, verifier, logger),
        ("persist", paths.data_root, plan, "verified", clock),
        ("state", logger, "verified"),
    ]


def test_upload_final_metadata_returns_existing_revision_before_upload(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    paths = _paths(tmp_path)
    data_dir = paths.data_root / "data"
    data_dir.mkdir()
    (data_dir / "region.parquet").write_bytes(b"parquet")
    plan = _metadata_plan(paths.data_root)
    monkeypatch.setattr(finalization_module, "_build_metadata_only_upload_plan", lambda _root: plan)
    monkeypatch.setattr(finalization_module, "_metadata_state_matches", lambda _root, _plan: True)
    monkeypatch.setattr(finalization_module, "_metadata_skip_revision", lambda *_args: "existing")
    monkeypatch.setattr(
        finalization_module,
        "_upload_metadata",
        lambda *_args: pytest.fail("matching metadata must not upload"),
    )

    assert (
        upload_final_metadata(
            paths,
            verifier=None,
            upload_runner=None,
            upload_timeout=None,
            clock=lambda: "now",
            plan_validator=lambda _root: None,
        )
        == "existing"
    )


def test_upload_metadata_delegates_and_logs_completion(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    plan = _metadata_plan(tmp_path)
    paths = Paths(tmp_path / "raw", tmp_path)
    logger = _EventLogger()
    upload_runner = object()
    calls: list[tuple[object, ...]] = []
    monkeypatch.setattr(
        finalization_module,
        "_run_metadata_upload",
        lambda *args: calls.append(args),
    )

    _upload_metadata(plan, paths, upload_runner, 4.0, logger)  # type: ignore[arg-type]

    assert calls == [(plan, paths, upload_runner, 4.0, logger)]
    assert logger.events == [("metadata_upload_complete", {"level": "INFO"})]


@pytest.mark.parametrize(
    "error",
    [
        PublicationError("publication"),
        subprocess.CalledProcessError(1, ["hf"]),
        subprocess.TimeoutExpired(["hf"], 1),
    ],
)
def test_upload_metadata_wraps_expected_upload_failures(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, error: Exception
) -> None:
    plan = _metadata_plan(tmp_path)
    paths = Paths(tmp_path / "raw", tmp_path)
    monkeypatch.setattr(
        finalization_module,
        "_run_metadata_upload",
        lambda *_args: (_ for _ in ()).throw(error),
    )

    with pytest.raises(OrchestratorError, match="final metadata upload failed"):
        _upload_metadata(plan, paths, None, None, None)


def test_verify_metadata_requires_verifier_and_logs_success(tmp_path: Path) -> None:
    plan = _metadata_plan(tmp_path)
    with pytest.raises(
        OrchestratorError,
        match=r"^no Hub verifier supplied; cannot record final revision$",
    ):
        _verify_metadata(plan, None, None)

    logger = _EventLogger()
    assert _verify_metadata(plan, lambda _repo, _files: "verified", logger) == "verified"
    assert logger.events == [
        ("metadata_verification_start", {"level": "INFO"}),
        (
            "metadata_verification_complete",
            {"level": "INFO", "verified_revision": "verified"},
        ),
    ]


def test_completeness_sets_inspects_artifacts_in_sorted_order(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    paths = _paths(tmp_path)
    data_dir = paths.data_root / "data"
    data_dir.mkdir()
    (data_dir / "b.parquet").write_bytes(b"b")
    (data_dir / "a.parquet").write_bytes(b"a")
    discovered: dict[str, Source] = {}
    calls: list[tuple[Paths, Path, dict[str, Source]]] = []

    def inspect(
        _paths: Paths,
        parquet: Path,
        _discovered: dict[str, Source],
    ) -> tuple[str | None, str | None, str | None]:
        calls.append((_paths, parquet, _discovered))
        return {
            "a.parquet": ("a.osm.pbf", None, None),
            "b.parquet": (None, "b", "missing"),
        }[parquet.name]

    monkeypatch.setattr(finalization_module, "_inspect_artifact", inspect)

    assert _completeness_sets(paths, discovered) == (
        {"a.osm.pbf"},
        ["b"],
        ["missing"],
    )
    assert calls == [
        (paths, data_dir / "a.parquet", discovered),
        (paths, data_dir / "b.parquet", discovered),
    ]


def test_inspect_artifact_reports_unknown_and_missing_manifest(tmp_path: Path) -> None:
    paths = _paths(tmp_path)
    parquet = paths.data_root / "data" / "unknown.parquet"
    parquet.parent.mkdir()
    parquet.write_bytes(b"parquet")
    assert _inspect_artifact(paths, parquet, {}) == (None, "unknown", None)

    source = _source(tmp_path)
    known = paths.data_root / "data" / "region.parquet"
    known.write_bytes(b"parquet")
    assert _inspect_artifact(paths, known, {source.name: source}) == (
        None,
        None,
        "region.manifest.json",
    )


def test_inspect_artifact_reports_invalid_and_non_resumable_artifacts(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    paths = _paths(tmp_path)
    source = _source(tmp_path)
    parquet = paths.data_root / "data" / "region.parquet"
    parquet.parent.mkdir()
    parquet.write_bytes(b"parquet")
    manifest_path = paths.data_root / "manifests" / "region.manifest.json"
    manifest_path.parent.mkdir()
    manifest_path.write_text("{}", encoding="utf-8")
    discovered = {source.name: source}

    validation_paths: list[Path] = []
    manifest_paths: list[Path] = []

    def validate(path: Path) -> None:
        validation_paths.append(path)

    manifest = SimpleNamespace(name="manifest")

    def read_manifest(path: Path) -> SimpleNamespace:
        manifest_paths.append(path)
        return manifest

    monkeypatch.setattr(finalization_module, "validate_geoparquet", validate)
    monkeypatch.setattr(
        finalization_module,
        "read_manifest",
        read_manifest,
    )
    source_identity_paths: list[Path] = []
    output_identity_paths: list[Path] = []

    def source_identity(path: Path) -> tuple[str, Path]:
        source_identity_paths.append(path)
        return ("source", path)

    def output_identity(path: Path) -> tuple[str, Path]:
        output_identity_paths.append(path)
        return ("output", path)

    monkeypatch.setattr(finalization_module, "source_identity_for", source_identity)
    monkeypatch.setattr(finalization_module, "output_identity_for", output_identity)
    resumable_calls: list[tuple[object, object, object]] = []
    resumable = True

    def is_resumable(
        actual_manifest: object,
        actual_source_identity: object,
        actual_output_identity: object,
    ) -> bool:
        resumable_calls.append((actual_manifest, actual_source_identity, actual_output_identity))
        return resumable

    monkeypatch.setattr(
        finalization_module,
        "is_resumable",
        is_resumable,
    )

    assert _inspect_artifact(paths, parquet, discovered) == (source.name, None, None)
    assert validation_paths == [parquet]
    assert manifest_paths == [manifest_path]
    assert source_identity_paths == [source.path]
    assert output_identity_paths == [parquet]
    assert resumable_calls == [(manifest, ("source", source.path), ("output", parquet))]
    resumable = False
    assert _inspect_artifact(paths, parquet, discovered) == (
        None,
        None,
        "region (not resumable)",
    )

    monkeypatch.setattr(
        finalization_module,
        "validate_geoparquet",
        lambda _path: (_ for _ in ()).throw(StorageError("invalid")),
    )
    assert _inspect_artifact(paths, parquet, discovered) == (
        None,
        None,
        "region (invalid manifest or parquet)",
    )


def test_inspect_artifact_propagates_unexpected_validation_errors(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    paths = _paths(tmp_path)
    source = _source(tmp_path)
    parquet = paths.data_root / "data" / "region.parquet"
    parquet.parent.mkdir(parents=True)
    parquet.write_bytes(b"parquet")
    manifest_path = paths.data_root / "manifests" / "region.manifest.json"
    manifest_path.parent.mkdir(parents=True)
    manifest_path.write_text("{}", encoding="utf-8")

    def fail_unexpectedly(_path: Path) -> None:
        raise RuntimeError("unexpected validator failure")

    monkeypatch.setattr(finalization_module, "validate_geoparquet", fail_unexpectedly)

    with pytest.raises(RuntimeError, match="unexpected validator failure"):
        _inspect_artifact(paths, parquet, {source.name: source})


def test_verify_final_completeness_reports_all_incomplete_categories(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    paths = _paths(tmp_path)
    sources = [_source(tmp_path)]
    monkeypatch.setattr(
        finalization_module,
        "_completeness_sets",
        lambda _paths, _discovered: ({"completed"}, ["extra"], ["incomplete"]),
    )

    with pytest.raises(
        OrchestratorError,
        match=r"missing=\['region\.osm\.pbf'\].*extra=\['extra'\].*incomplete=\['incomplete'\]",
    ) as exc_info:
        verify_final_completeness(paths, sources)
    assert str(exc_info.value) == (
        "final completeness failed: missing=['region.osm.pbf'] "
        "extra=['extra'] incomplete=['incomplete']"
    )


@pytest.mark.parametrize(
    "extra_and_incomplete",
    [
        (["extra"], []),
        ([], ["incomplete"]),
    ],
)
def test_verify_final_completeness_rejects_extra_or_incomplete_alone(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    extra_and_incomplete: tuple[list[str], list[str]],
) -> None:
    paths = _paths(tmp_path)
    source = _source(tmp_path)
    extra, incomplete = extra_and_incomplete
    monkeypatch.setattr(
        finalization_module,
        "_completeness_sets",
        lambda *_args: ({source.name}, extra, incomplete),
    )

    with pytest.raises(OrchestratorError, match="^final completeness failed:"):
        verify_final_completeness(paths, [source])


def test_append_if_present_supports_set_list_and_none() -> None:
    values: set[str] = set()
    _append_if_present(values, "value")
    _append_if_present(values, None)
    ordered: list[str] = []
    _append_if_present(ordered, "value")
    _append_if_present(ordered, None)
    assert values == {"value"}
    assert ordered == ["value"]


def test_persist_metadata_state_records_all_managed_artifact_identities(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    files = {
        "README.md": b"readme",
        "stats.json": b"stats",
        H3_MAP_ASSET_RELATIVE: b"map",
        AREA_HISTOGRAM_ASSET_RELATIVE: b"histogram",
        DATASET_CARD_HERO_ASSET_RELATIVE: b"hero",
    }
    for relative, content in files.items():
        path = tmp_path / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(content)
    captured: dict[str, object] = {}

    def write_state(data_root: Path, **kwargs: object) -> None:
        captured["data_root"] = data_root
        captured.update(kwargs)

    seen_paths: list[Path] = []
    real_file_sha256 = finalization_module.file_sha256

    def file_hash(path: Path) -> str:
        seen_paths.append(path)
        return real_file_sha256(path)

    monkeypatch.setattr(finalization_module, "_write_metadata_state", write_state)
    monkeypatch.setattr(finalization_module, "file_sha256", file_hash)

    _persist_metadata_state(tmp_path, _metadata_plan(tmp_path), "remote-revision", lambda: "now")

    assert captured == {
        "data_root": tmp_path,
        "identity_sha256": "plan-id",
        "readme_sha256": hashlib.sha256(b"readme").hexdigest(),
        "stats_sha256": hashlib.sha256(b"stats").hexdigest(),
        "readme_size_bytes": 6,
        "stats_size_bytes": 5,
        "h3_map_sha256": hashlib.sha256(b"map").hexdigest(),
        "h3_map_size_bytes": 3,
        "area_histogram_sha256": hashlib.sha256(b"histogram").hexdigest(),
        "area_histogram_size_bytes": 9,
        "dataset_card_hero_sha256": hashlib.sha256(b"hero").hexdigest(),
        "dataset_card_hero_size_bytes": 4,
        "verified_revision": "remote-revision",
        "completed_at": "now",
    }
    assert seen_paths == [
        tmp_path / "README.md",
        tmp_path / "stats.json",
        tmp_path / H3_MAP_ASSET_RELATIVE,
        tmp_path / AREA_HISTOGRAM_ASSET_RELATIVE,
        tmp_path / DATASET_CARD_HERO_ASSET_RELATIVE,
    ]


def test_metadata_skip_revision_returns_revision_and_logs_when_state_matches(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    events: list[tuple[str, dict[str, object]]] = []
    plan = _metadata_plan(tmp_path)
    matches: list[tuple[Path, UploadPlan]] = []

    class Logger:
        def event(self, name: str, **fields: object) -> None:
            events.append((name, fields))

    def state_matches(root: Path, actual_plan: UploadPlan) -> bool:
        matches.append((root, actual_plan))
        return True

    monkeypatch.setattr(
        "osm_polygon_description_tag.workflow.finalization._metadata_state_matches",
        state_matches,
    )
    seen_roots: list[Path] = []

    def read_state(root: Path) -> dict[str, object]:
        seen_roots.append(root)
        return {"metadata": {"verified_revision": "revision"}}

    monkeypatch.setattr(
        "osm_polygon_description_tag.workflow.finalization._read_publication_state",
        read_state,
    )

    result = _metadata_skip_revision(tmp_path, plan, Logger())

    assert result == "revision"
    assert matches == [(tmp_path, plan)]
    assert seen_roots == [tmp_path]
    assert events == [("metadata_skip", {"level": "INFO", "verified_revision": "revision"})]


def test_metadata_skip_revision_logs_empty_revision_when_metadata_is_absent(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    logger = _EventLogger()
    monkeypatch.setattr(finalization_module, "_metadata_state_matches", lambda *_args: True)
    monkeypatch.setattr(
        finalization_module,
        "_read_publication_state",
        lambda _root: {"metadata": {}},
    )

    assert _metadata_skip_revision(tmp_path, _metadata_plan(tmp_path), logger) is None
    assert logger.events == [("metadata_skip", {"level": "INFO", "verified_revision": ""})]


def test_metadata_skip_revision_treats_missing_metadata_as_empty_state(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setattr(finalization_module, "_metadata_state_matches", lambda *_args: True)
    monkeypatch.setattr(finalization_module, "_read_publication_state", lambda _root: {})

    assert _metadata_skip_revision(tmp_path, _metadata_plan(tmp_path), None) is None


def test_metadata_skip_revision_does_not_read_state_when_identity_differs(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setattr(
        "osm_polygon_description_tag.workflow.finalization._metadata_state_matches",
        lambda _root, _plan: False,
    )
    monkeypatch.setattr(
        "osm_polygon_description_tag.workflow.finalization._read_publication_state",
        lambda _root: pytest.fail("state must not be read"),
    )

    assert _metadata_skip_revision(tmp_path, _metadata_plan(tmp_path), None) is None


def test_run_metadata_upload_uses_default_executor_with_confirmation(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    plan = _metadata_plan(tmp_path)
    seen: dict[str, object] = {}

    def execute(upload_plan: UploadPlan, **kwargs: object) -> None:
        seen["plan"] = upload_plan
        seen.update(kwargs)

    monkeypatch.setattr("osm_polygon_description_tag.workflow.finalization.execute_upload", execute)

    logger = _EventLogger()
    _run_metadata_upload(plan, Paths(tmp_path / "raw", tmp_path), None, 12.0, logger)

    assert seen == {
        "plan": plan,
        "confirmation": "plan-id",
        "timeout": 12.0,
        "retry_observer": seen["retry_observer"],
    }
    assert callable(seen["retry_observer"])
    assert logger.events == []


def test_run_metadata_upload_accepts_injected_runner_and_rejects_empty_revision(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    plan = _metadata_plan(tmp_path)
    paths = Paths(tmp_path / "raw", tmp_path)
    command = ["hf", "upload"]
    seen_commands: list[list[str]] = []

    def command_for(root: Path) -> list[str]:
        assert root == tmp_path
        return command

    monkeypatch.setattr(
        "osm_polygon_description_tag.workflow.finalization.metadata_only_command",
        command_for,
    )

    def runner(actual_command: list[str]) -> str:
        seen_commands.append(actual_command)
        return "revision"

    assert _run_metadata_upload(plan, paths, runner, None, None) is None
    assert seen_commands == [command]

    with pytest.raises(PublicationError, match=r"^upload runner returned empty revision$"):
        _run_metadata_upload(plan, paths, lambda _actual: "", None, None)


def test_call_metadata_verifier_forwards_repo_and_files_and_wraps_failures() -> None:
    plan = _metadata_plan(Path("/data"))
    seen: list[tuple[str, tuple[UploadItem, ...]]] = []

    def verifier(repo_id: str, files: tuple[UploadItem, ...]) -> str:
        seen.append((repo_id, files))
        return "revision"

    assert _call_metadata_verifier(verifier, plan) == "revision"
    assert seen == [(plan.repo_id, plan.files)]

    with pytest.raises(
        OrchestratorError,
        match=r"^Hub verifier returned no revision for final metadata$",
    ):
        _call_metadata_verifier(lambda *_args: "", plan)
    with pytest.raises(OrchestratorError, match="final Hub verifier failed"):
        _call_metadata_verifier(lambda *_args: (_ for _ in ()).throw(ValueError("broken")), plan)


def test_metadata_retry_observer_forwards_stage_and_fields() -> None:
    events: list[tuple[str, dict[str, object]]] = []

    class Logger:
        def event(self, name: str, **fields: object) -> None:
            events.append((name, fields))

    observer = _metadata_retry_observer(Logger())
    assert observer is not None
    observer(attempt=2, reason="timeout")
    assert events == [("upload_retry", {"stage": "metadata", "attempt": 2, "reason": "timeout"})]
    assert _metadata_retry_observer(None) is None


def test_metadata_log_helpers_emit_exact_events_when_logger_exists() -> None:
    logger = _EventLogger()

    _log_metadata_start(logger)
    _log_metadata_start(None)
    _log_metadata_state(logger, "revision")
    _log_metadata_state(None, "revision")

    assert logger.events == [
        ("metadata_upload_start", {"level": "INFO"}),
        (
            "metadata_state_written",
            {"level": "INFO", "verified_revision": "revision"},
        ),
    ]
