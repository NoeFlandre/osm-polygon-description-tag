import pyarrow as pa

from osm_polygon_description_tag.schema import SCHEMA, SCHEMA_VERSION, geo_metadata


def test_arrow_schema_is_frozen() -> None:
    assert SCHEMA.names == [
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


def test_arrow_field_types_and_nullability() -> None:
    assert SCHEMA.field("osm_id").type == pa.int64()
    assert SCHEMA.field("osm_id").nullable is False
    assert SCHEMA.field("version").type == pa.int32()
    assert SCHEMA.field("timestamp").type == pa.timestamp("ms", tz="UTC")
    assert SCHEMA.field("area_m2").type == pa.float64()
    assert SCHEMA.field("geometry").type == pa.binary()
    assert SCHEMA.field("geometry").nullable is False
    assert SCHEMA.field("tags").type.key_type == pa.string()
    assert SCHEMA.field("tags").type.item_type == pa.string()
    assert SCHEMA.field("tags").nullable is False
    assert SCHEMA.field("localized_descriptions").nullable is False
    assert SCHEMA.field("localized_names").nullable is False
    assert SCHEMA.field("description").nullable is True
    assert SCHEMA.field("name").nullable is True
    assert SCHEMA.field("version").nullable is True


def test_schema_version_is_pinned() -> None:
    assert SCHEMA_VERSION == 2


def test_geo_metadata_is_geoparquet_1_1() -> None:
    metadata = geo_metadata(["Polygon", "MultiPolygon"], [-1.0, -2.0, 3.0, 4.0])
    assert metadata["version"] == "1.1.0"
    assert metadata["primary_column"] == "geometry"
    geometry_meta = metadata["columns"]["geometry"]
    assert geometry_meta["encoding"] == "WKB"
    assert geometry_meta["geometry_types"] == ["MultiPolygon", "Polygon"]
    assert geometry_meta["bbox"] == [-1.0, -2.0, 3.0, 4.0]
    assert "crs" not in geometry_meta
    covering = geometry_meta["covering"]["bbox"]
    assert covering["xmin"] == ["bbox_min_x"]
    assert covering["ymax"] == ["bbox_max_y"]
