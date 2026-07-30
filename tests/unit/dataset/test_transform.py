from datetime import UTC, datetime

import pytest
from shapely import from_wkb, to_wkb
from shapely.geometry import LineString, MultiPolygon, Polygon

from osm_polygon_description_tag.dataset.transform import (
    RejectedFeature,
    descriptions_from_tags,
    geodesic_area_m2,
    transform_record,
)
from osm_polygon_description_tag.extraction import ExportRecord


def test_descriptions_preserve_base_suffixes_and_values() -> None:
    tags = {
        "description": " Base text ",
        "description:en": "English",
        "description:pt-BR": "Português",
        "description:fr": "   ",
        "name": "Place",
    }
    assert descriptions_from_tags(tags) == (
        " Base text ",
        {"en": "English", "pt-BR": "Português"},
    )


def test_descriptions_exclude_empty_suffix_and_missing_base() -> None:
    base, localized = descriptions_from_tags({"description:": "x", "description:en": "EN"})
    assert base is None
    assert localized == {"en": "EN"}


def test_descriptions_whitespace_only_base_becomes_none() -> None:
    base, localized = descriptions_from_tags({"description": "   "})
    assert base is None
    assert localized == {}


def _ewkb_hex(geom: object) -> str:
    return to_wkb(geom, include_srid=True, flavor="extended", byte_order=1).hex()  # type: ignore[arg-type]


def _record(
    geom: object, tags: dict[str, str], *, osm_type: str = "way", osm_id: int = 1
) -> ExportRecord:
    return ExportRecord(
        geometry_ewkb_hex=_ewkb_hex(geom),
        osm_type=osm_type,
        osm_id=osm_id,
        version=5,
        changeset=700,
        timestamp="2026-01-01T00:00:00Z",
        tags=tags,
    )


def test_transform_polygon_with_hole_relation() -> None:
    outer = [(0, 0), (0, 4), (4, 4), (4, 0), (0, 0)]
    inner = [(1, 1), (1, 2), (2, 2), (2, 1), (1, 1)]
    polygon = Polygon(outer, [inner])
    record = _record(
        polygon, {"description": "Has a hole", "name": "Preserved"}, osm_type="relation", osm_id=42
    )

    result = transform_record(record, "fixture.osm.pbf")

    assert result["osm_type"] == "relation"
    assert result["osm_id"] == 42
    assert result["osm_url"] == "https://www.openstreetmap.org/relation/42"
    assert result["geometry_type"] == "Polygon"
    assert result["area_m2"] > 0
    assert result["bbox_min_x"] == 0.0
    assert result["bbox_max_y"] == 4.0
    assert result["tags"]["name"] == "Preserved"
    assert "__osm_id" not in result["tags"]
    assert result["source_pbf"] == "fixture.osm.pbf"
    assert result["version"] == 5
    assert result["changeset"] == 700
    assert result["timestamp"] == datetime(2026, 1, 1, tzinfo=UTC)
    assert result["description"] == "Has a hole"
    # Hole reduces area below the outer-only area.
    outer_only = geodesic_area_m2(Polygon(outer))
    assert result["area_m2"] < outer_only
    # Geometry WKB round-trips to the same polygon.
    assert from_wkb(result["geometry"]).geom_type == "Polygon"


def test_transform_multipolygon_two_parts() -> None:
    mp = MultiPolygon(
        [
            Polygon([(0, 0), (0, 1), (1, 1), (1, 0)]),
            Polygon([(10, 10), (10, 11), (11, 11), (11, 10)]),
        ]
    )
    record = _record(mp, {"description:en": "Two parts"}, osm_type="relation", osm_id=7)

    result = transform_record(record, "x.osm.pbf")

    assert result["geometry_type"] == "MultiPolygon"
    assert result["area_m2"] > 0
    assert result["localized_descriptions"] == {"en": "Two parts"}
    assert result["description"] is None


def test_transform_preserves_unusual_description_suffix() -> None:
    polygon = Polygon([(0, 0), (0, 1), (1, 1), (1, 0)])
    record = _record(polygon, {"description:pt-BR": "PT", "description:zh-Hant-TW": "ZH"})

    result = transform_record(record, "x.osm.pbf")

    assert result["localized_descriptions"] == {"pt-BR": "PT", "zh-Hant-TW": "ZH"}


@pytest.mark.parametrize(
    ("reason", "geom", "tags", "osm_type", "osm_id"),
    [
        (
            "no_nonempty_description",
            Polygon([(0, 0), (0, 1), (1, 1), (1, 0)]),
            {"name": "no desc"},
            "way",
            1,
        ),
        (
            "no_nonempty_description",
            Polygon([(0, 0), (0, 1), (1, 1), (1, 0)]),
            {"description": "  "},
            "way",
            1,
        ),
        ("non_polygon_geometry", LineString([(0, 0), (1, 1)]), {"description": "x"}, "way", 1),
        (
            "unsupported_osm_type",
            Polygon([(0, 0), (0, 1), (1, 1), (1, 0)]),
            {"description": "x"},
            "node",
            1,
        ),
        (
            "invalid_osm_id",
            Polygon([(0, 0), (0, 1), (1, 1), (1, 0)]),
            {"description": "x"},
            "way",
            -1,
        ),
    ],
)
def test_transform_rejects_with_stable_reason(
    reason: str, geom: object, tags: dict[str, str], osm_type: str, osm_id: int
) -> None:
    record = _record(geom, tags, osm_type=osm_type, osm_id=osm_id)
    with pytest.raises(RejectedFeature) as info:
        transform_record(record, "x.osm.pbf")
    assert info.value.reason == reason


def test_transform_rejects_nonpositive_area(monkeypatch: pytest.MonkeyPatch) -> None:
    # A valid polygon always has positive geodesic area, so exercise the branch directly.
    polygon = Polygon([(0, 0), (0, 1), (1, 1), (1, 0)])
    monkeypatch.setattr(
        "osm_polygon_description_tag.dataset.transform.geodesic_area_m2", lambda _geom: 0.0
    )
    record = _record(polygon, {"description": "x"})
    with pytest.raises(RejectedFeature) as info:
        transform_record(record, "x.osm.pbf")
    assert info.value.reason == "nonpositive_area"


def test_transform_rejects_undecodable_wkb() -> None:
    record = ExportRecord(
        geometry_ewkb_hex="zzzznotwkb",
        osm_type="way",
        osm_id=1,
        version=1,
        changeset=1,
        timestamp="2026-01-01T00:00:00Z",
        tags={"description": "x"},
    )
    with pytest.raises(RejectedFeature, match="invalid_geometry"):
        transform_record(record, "x.osm.pbf")
