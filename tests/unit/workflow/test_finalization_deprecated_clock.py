"""``refresh_dataset_docs`` keeps accepting, and ignoring, its deprecated ``clock``."""

from io import StringIO
from pathlib import Path

from osm_polygon_description_tag.runtime.config import Paths
from osm_polygon_description_tag.runtime.logging import RunLogger
from osm_polygon_description_tag.workflow.finalization import refresh_dataset_docs


def test_refresh_docs_still_accepts_and_ignores_the_deprecated_clock(tmp_path: Path) -> None:
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

    def clock() -> str:
        raise AssertionError("the deprecated clock must never be called")

    def generator(root: Path, _template: Path) -> None:
        calls.append(root)

    refresh_dataset_docs(paths, clock=clock, logger=logger, docs_generator=generator)
    logger.close()

    assert calls == [data_root]
