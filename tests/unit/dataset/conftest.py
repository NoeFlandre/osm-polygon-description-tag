"""Shared schema-valid records for dataset unit tests."""

from collections.abc import Iterator

import pytest
from shapely.geometry import MultiPolygon, Polygon

from tests.conftest import make_record_dict


@pytest.fixture
def way_record_dict() -> dict[str, object]:
    return make_record_dict(
        Polygon([(0, 0), (0, 1), (1, 1), (1, 0)]),
        {"description": "A building", "building": "yes"},
        osm_type="way",
        osm_id=100,
    )


@pytest.fixture
def relation_record_dict() -> dict[str, object]:
    geom = MultiPolygon(
        [
            Polygon([(10, 10), (10, 11), (11, 11), (11, 10)]),
            Polygon([(20, 20), (20, 21), (21, 21), (21, 20)]),
        ]
    )
    return make_record_dict(
        geom,
        {"description:en": "Two parts", "description:pt-BR": "Duas partes"},
        osm_type="relation",
        osm_id=200,
    )


@pytest.fixture
def valid_records(
    way_record_dict: dict[str, object], relation_record_dict: dict[str, object]
) -> list[dict[str, object]]:
    return [way_record_dict, relation_record_dict]


@pytest.fixture
def record_stream(valid_records: list[dict[str, object]]) -> Iterator[dict[str, object]]:
    yield from valid_records
