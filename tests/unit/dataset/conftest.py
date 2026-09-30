"""Shared schema-valid records for dataset unit tests."""

from collections.abc import Callable, Iterator

import pytest
from shapely.geometry import MultiPolygon, Polygon

from osm_polygon_description_tag.dataset.manifest import (
    MANIFEST_SCHEMA_VERSION,
    Manifest,
    OutputIdentity,
    RunCounts,
    SourceIdentity,
    current_area_policy_sha256,
    current_output_algorithm_revision,
)
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


@pytest.fixture
def manifest_factory() -> Callable[..., Manifest]:
    """Build a current-schema manifest with explicit source/output identities."""

    def build(
        *,
        source: SourceIdentity,
        output: OutputIdentity,
        code_revision: str | None = None,
        output_algorithm_revision: str | None = None,
    ) -> Manifest:
        return Manifest(
            manifest_schema_version=MANIFEST_SCHEMA_VERSION,
            schema_version=3,
            geoparquet_version="1.1.0",
            transform_algorithm_version=3,
            area_policy_sha256=current_area_policy_sha256(),
            output_algorithm_revision=(
                current_output_algorithm_revision()
                if output_algorithm_revision is None
                else output_algorithm_revision
            ),
            source=source,
            output=output,
            osmium_version="osmium version 1.19.1",
            dependency_versions={"pyarrow": "20.0.0"},
            code_revision=code_revision,
            started_at="2026-07-27T00:00:00+00:00",
            completed_at="2026-07-27T00:01:00+00:00",
            counts=RunCounts(emitted_features=1, included_rows=1, rejections={}),
        )

    return build
