"""Focused contracts for workflow finalization boundaries."""

from io import StringIO
from pathlib import Path

import pytest

from osm_polygon_description_tag.runtime.config import Paths
from osm_polygon_description_tag.runtime.logging import RunLogger
from osm_polygon_description_tag.workflow.finalization import (
    OrchestratorError,
    _cast_dict,
    _read_publication_state,
    _write_metadata_state,
    refresh_dataset_docs,
    upload_final_metadata,
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


def test_state_wrappers_translate_malformed_publication_state(tmp_path: Path) -> None:
    state_path = tmp_path / "publication-state.json"
    state_path.write_text("[]", encoding="utf-8")
    with pytest.raises(OrchestratorError):
        _read_publication_state(tmp_path)
    with pytest.raises(OrchestratorError):
        _cast_dict("not-a-dict")


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
