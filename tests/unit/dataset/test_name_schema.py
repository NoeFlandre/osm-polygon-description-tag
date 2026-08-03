"""Arrow schema and transformation contract for the amendment dataset.

These tests assert the public first-class name and localized_names columns.
The schema and transform algorithm versions are bumped to 3 for the
Hugging Face-compatible key/value list representation.

Contract:

- ``name`` is a nullable string containing the exact ``name`` value.
- ``localized_names`` is a non-null list of key/value records containing exact
  non-empty ``name:<suffix>`` values keyed by the unmodified suffix.
- ``tags`` remains complete and authoritative.
- ``description`` and ``localized_descriptions`` retain their semantics.
"""

from __future__ import annotations

import pyarrow as pa
from shapely import to_wkb
from shapely.geometry import MultiPolygon, Polygon

from osm_polygon_description_tag.dataset.schema import SCHEMA, SCHEMA_VERSION
from osm_polygon_description_tag.dataset.transform import (
    names_from_tags,
    transform_record,
)
from osm_polygon_description_tag.extraction import ExportRecord


def _record(
    geom: object,
    tags: dict[str, str],
    *,
    osm_type: str = "way",
    osm_id: int = 1,
) -> ExportRecord:
    ewkb = to_wkb(geom, include_srid=True, flavor="extended", byte_order=1)
    return ExportRecord(
        geometry_ewkb_hex=ewkb.hex(),
        osm_type=osm_type,
        osm_id=osm_id,
        version=1,
        changeset=1,
        timestamp="2026-01-01T00:00:00Z",
        tags=tags,
    )


def test_schema_version_is_bumped_to_three() -> None:
    """The viewer-compatible representation requires a schema version bump."""
    assert SCHEMA_VERSION == 3
    assert SCHEMA.field("name") == pa.field("name", pa.string(), nullable=True)
    assert SCHEMA.field("localized_names") == pa.field(
        "localized_names",
        pa.list_(
            pa.struct(
                [pa.field("key", pa.string(), nullable=False), pa.field("value", pa.string())]
            )
        ),
        nullable=False,
    )


def test_schema_includes_name_columns_in_expected_order() -> None:
    expected = [
        "source_pbf",
        "osm_type",
        "osm_id",
        "osm_url",
        "version",
        "changeset",
        "timestamp",
        "name",
        "localized_names",
        "description",
        "localized_descriptions",
        "tags",
        "geometry_type",
        "area_m2",
        "bbox_min_x",
        "bbox_min_y",
        "bbox_max_x",
        "bbox_max_y",
        "geometry",
    ]
    assert SCHEMA.names == expected


def test_names_from_tags_returns_base_and_localized() -> None:
    base, localized = names_from_tags({"name": "Polygons Building", "name:pt-BR": "Prédio"})
    assert base == "Polygons Building"
    assert localized == {"pt-BR": "Prédio"}


def test_names_from_tags_returns_none_for_missing_name() -> None:
    base, localized = names_from_tags({"name:en": "EN name"})
    assert base is None
    assert localized == {"en": "EN name"}


def test_names_from_tags_preserves_unusual_suffixes() -> None:
    base, localized = names_from_tags(
        {
            "name": "Base",
            "name:zh-Hant-TW": "TW",
            "name:en-GB-x-oed": "OXED",
        }
    )
    assert base == "Base"
    assert localized == {"zh-Hant-TW": "TW", "en-GB-x-oed": "OXED"}


def test_names_from_tags_rejects_empty_values() -> None:
    base, localized = names_from_tags({"name": "  ", "name:en": "EN", "name:": "bad"})
    assert base is None
    assert localized == {"en": "EN"}


def test_transform_record_emits_name_columns() -> None:
    polygon = Polygon([(0, 0), (0, 1), (1, 1), (1, 0)])
    record = _record(
        polygon,
        {
            "name": "Building",
            "name:pt-BR": "Prédio",
            "description": "With name",
            "building": "yes",
        },
    )
    result = transform_record(record, "x.osm.pbf")
    assert result["name"] == "Building"
    assert result["localized_names"] == {"pt-BR": "Prédio"}
    assert result["description"] == "With name"
    assert result["tags"] == {
        "name": "Building",
        "name:pt-BR": "Prédio",
        "description": "With name",
        "building": "yes",
    }


def test_transform_record_agrees_with_tags_map_for_ways() -> None:
    polygon = Polygon([(0, 0), (0, 1), (1, 1), (1, 0)])
    record = _record(
        polygon,
        {"name": "Park", "name:de": "Park Deutsch", "description": "City park"},
    )
    result = transform_record(record, "x.osm.pbf")
    assert result["name"] == result["tags"]["name"]
    expected_localized = {
        k.removeprefix("name:"): v
        for k, v in result["tags"].items()
        if k.startswith("name:") and k != "name:" and v.strip()
    }
    assert result["localized_names"] == expected_localized  # type: ignore[union-attr]


def test_transform_record_agrees_with_tags_map_for_relations() -> None:
    mp = MultiPolygon(
        [
            Polygon([(0, 0), (0, 1), (1, 1), (1, 0)]),
            Polygon([(10, 10), (10, 11), (11, 11), (11, 10)]),
        ]
    )
    record = _record(
        mp,
        {"name": "Lake", "name:en": "Lake EN", "description": "Twin lake"},
        osm_type="relation",
        osm_id=42,
    )
    result = transform_record(record, "x.osm.pbf")
    assert result["name"] == result["tags"]["name"]
    expected_localized = {
        k.removeprefix("name:"): v
        for k, v in result["tags"].items()
        if k.startswith("name:") and k != "name:" and v.strip()
    }
    assert result["localized_names"] == expected_localized  # type: ignore[union-attr]


def test_transform_record_missing_name_columns() -> None:
    polygon = Polygon([(0, 0), (0, 1), (1, 1), (1, 0)])
    record = _record(polygon, {"description": "x", "building": "yes"})
    result = transform_record(record, "x.osm.pbf")
    assert result["name"] is None
    assert result["localized_names"] == {}


def test_arrow_map_array_round_trip() -> None:
    """PyArrow serialises a non-empty localized_names map as a list of pairs."""
    polygon = Polygon([(0, 0), (0, 1), (1, 1), (1, 0)])
    record = _record(
        polygon,
        {"name": "X", "name:pt-BR": "X BR", "description": "y"},
    )
    result = transform_record(record, "x.osm.pbf")
    from osm_polygon_description_tag.dataset.schema import mapping_to_pairs

    arrow_result = dict(result)
    for column in ("localized_names", "localized_descriptions", "tags"):
        arrow_result[column] = mapping_to_pairs(result[column])
    batch = pa.RecordBatch.from_pylist([arrow_result], schema=SCHEMA)
    table = pa.Table.from_batches([batch])
    localized = table.column("localized_names").to_pylist()
    assert localized == [[{"key": "pt-BR", "value": "X BR"}]]


def test_e2e_parquet_round_trip_preserves_name_columns(tmp_path) -> None:
    import pyarrow.parquet as pq

    from osm_polygon_description_tag.dataset.storage import write_geoparquet

    polygon = Polygon([(0, 0), (0, 1), (1, 1), (1, 0)])
    record = _record(
        polygon,
        {"name": "Round Trip", "name:fr": "Aller-Retour", "description": "Desc"},
    )
    output = tmp_path / "_amendment_e2e_parquet.parquet"
    write_geoparquet(iter([transform_record(record, "x.osm.pbf")]), output, batch_size=10)
    table = pq.read_table(output)
    assert "name" in table.column_names
    assert "localized_names" in table.column_names
    assert table.column("name").to_pylist() == ["Round Trip"]
    assert table.column("localized_names").to_pylist() == [[{"key": "fr", "value": "Aller-Retour"}]]
