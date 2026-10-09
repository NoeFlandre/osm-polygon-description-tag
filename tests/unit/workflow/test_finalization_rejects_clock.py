"""``refresh_dataset_docs`` refuses a ``clock`` argument, since docs carry no timestamp."""

from io import StringIO
from pathlib import Path

import pytest

from osm_polygon_description_tag.runtime.config import Paths
from osm_polygon_description_tag.runtime.logging import RunLogger
from osm_polygon_description_tag.workflow.finalization import refresh_dataset_docs


def test_refresh_docs_rejects_a_clock_argument(tmp_path: Path) -> None:
    source_root = tmp_path / "raw"
    data_root = tmp_path / "generated"
    source_root.mkdir()
    (data_root / "data").mkdir(parents=True)
    (data_root / "data" / "a.parquet").write_bytes(b"placeholder")
    paths = Paths(source_root=source_root, data_root=data_root)
    logger = RunLogger(
        data_root=tmp_path / "logs-root",
        run_id="test-run",
        clock=lambda: "2026-01-01T00:00:00+00:00",
        stderr=StringIO(),
    )
    calls: list[Path] = []

    def generator(root: Path, _template: Path) -> None:
        calls.append(root)

    try:
        with pytest.raises(TypeError, match="clock"):
            refresh_dataset_docs(
                paths,
                clock=lambda: "2026-01-01T00:00:00+00:00",  # ty: ignore[unknown-argument]
                logger=logger,
                docs_generator=generator,
            )
    finally:
        logger.close()

    assert calls == []
