"""Builders for synthetic GeoParquet shards used by language and grid tests."""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path

from shapely.geometry import Polygon

from osm_polygon_description_tag.dataset.storage import write_geoparquet
from tests.conftest import make_record_dict


def _default_tags(index: int) -> dict[str, str]:
    return {"description": f"A synthetic description number {index}"}


def write_description_shard(
    path: Path,
    count: int,
    *,
    start: int = 0,
    batch_size: int = 4,
    tags: Callable[[int], dict[str, str]] = _default_tags,
) -> None:
    """Write ``count`` unit-square rows with ``osm_id = index + 1`` to ``path``."""
    records = (
        make_record_dict(
            Polygon([(0, 0), (0, 1), (1, 1), (1, 0)]),
            tags(index),
            osm_id=index + 1,
        )
        for index in range(start, start + count)
    )
    path.parent.mkdir(parents=True, exist_ok=True)
    write_geoparquet(records, path, batch_size=batch_size)
