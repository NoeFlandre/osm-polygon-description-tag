"""Shared pytest fixtures producing realistic schema-conformant records."""

from collections.abc import Iterator

import pytest
from shapely import to_wkb
from shapely.geometry import MultiPolygon, Polygon

from osm_polygon_description_tag.extraction import ExportRecord
from osm_polygon_description_tag.transform import transform_record


def _ewkb_hex(geom: object) -> str:
    return to_wkb(geom, include_srid=True, flavor="extended", byte_order=1).hex()  # type: ignore[arg-type]


def make_export_record(
    geom: object,
    tags: dict[str, str],
    *,
    osm_type: str = "way",
    osm_id: int = 1,
) -> ExportRecord:
    return ExportRecord(
        geometry_ewkb_hex=_ewkb_hex(geom),
        osm_type=osm_type,
        osm_id=osm_id,
        version=1,
        changeset=10,
        timestamp="2026-01-01T00:00:00Z",
        tags=tags,
    )


def make_record_dict(
    geom: object,
    tags: dict[str, str],
    *,
    osm_type: str = "way",
    osm_id: int = 1,
    source_pbf: str = "region.osm.pbf",
) -> dict[str, object]:
    return transform_record(
        make_export_record(geom, tags, osm_type=osm_type, osm_id=osm_id), source_pbf
    )


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
