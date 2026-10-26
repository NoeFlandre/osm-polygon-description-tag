from __future__ import annotations

import re
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, call, patch

import pyarrow as pa
import pytest
from shapely import to_wkb
from shapely.geometry import MultiPolygon, Point, Polygon

import osm_polygon_description_tag.dataset.stats as stats_module
from osm_polygon_description_tag.dataset import stats_features, stats_geometry, stats_manifest
from osm_polygon_description_tag.dataset.text import successful_description_text_sql
from tests.helpers.messages import exactly


def test_collect_feature_summary_forwards_each_query_and_quantile_contract() -> None:
    connection = Mock()
    min_timestamp = datetime(2024, 1, 2, tzinfo=UTC)
    max_timestamp = datetime(2024, 2, 3, tzinfo=UTC)
    connection.execute.return_value.fetchone.return_value = (min_timestamp, max_timestamp)

    with (
        patch.object(
            stats_features,
            "query_int",
            side_effect=[7, 11, 9, 10, 3, 5, 4, 9],
        ) as query_int,
        patch.object(
            stats_features,
            "_ordered_counts",
            side_effect=[{"relation": 4}, {"Polygon": 8}],
        ) as ordered_counts,
        patch.object(
            stats_features,
            "_suffix_counts",
            side_effect=[{"en": 6}, {"fr": 7}],
        ) as suffix_counts,
        patch.object(
            stats_features,
            "_description_word_stats",
            side_effect=[(3, 30, 10.0), (4, 40, 11.0)],
        ) as word_stats,
        patch.object(
            stats_features,
            "_quantile_or_none",
            side_effect=[1.0, 2.0, 3.0, 4.0, 5.0],
        ) as quantile,
    ):
        summary = stats_features.collect_feature_summary(connection)

    assert summary.rows == 11
    assert summary.raw_successful_text_rows == 9
    assert summary.unique_osm_objects == 10
    assert summary.all_unique_osm_objects == 7
    assert summary.osm_types == {"relation": 4}
    assert summary.geometry_types == {"Polygon": 8}
    assert summary.description_suffixes == {"en": 6}
    assert summary.name_suffixes == {"fr": 7}
    assert summary.base_description_rows == 3
    assert summary.localized_description_rows == 5
    assert summary.base_name_rows == 4
    assert summary.localized_name_rows == 9
    assert summary.base_description_values == 3
    assert summary.localized_description_values == 4
    assert summary.area_min_m2 == 1.0
    assert summary.area_p25_m2 == 2.0
    assert summary.area_median_m2 == 3.0
    assert summary.area_p75_m2 == 4.0
    assert summary.area_max_m2 == 5.0
    assert summary.data_min_timestamp_utc == min_timestamp.isoformat()
    assert summary.data_max_timestamp_utc == max_timestamp.isoformat()

    assert query_int.call_args_list == [
        call(
            connection,
            "SELECT COUNT(*) FROM (SELECT DISTINCT osm_type, osm_id FROM all_features)",
        ),
        call(connection, "SELECT COUNT(*) FROM features"),
        call(
            connection,
            "SELECT COUNT(*) FROM all_features WHERE "  # noqa: S608 - internal fixed columns
            + successful_description_text_sql(localized_is_map=True),
        ),
        call(connection, "SELECT COUNT(*) FROM (SELECT DISTINCT osm_type, osm_id FROM features)"),
        call(connection, "SELECT COUNT(*) FROM features WHERE description IS NOT NULL"),
        call(
            connection,
            "SELECT COUNT(*) FROM features WHERE cardinality(localized_descriptions) > 0",
        ),
        call(connection, "SELECT COUNT(*) FROM features WHERE name IS NOT NULL"),
        call(connection, "SELECT COUNT(*) FROM features WHERE cardinality(localized_names) > 0"),
    ]
    assert ordered_counts.call_args_list == [
        call(
            connection,
            "SELECT osm_type, COUNT(*) FROM features GROUP BY osm_type ORDER BY osm_type",
        ),
        call(
            connection,
            "SELECT geometry_type, COUNT(*) FROM features GROUP BY geometry_type "
            "ORDER BY geometry_type",
        ),
    ]
    connection.execute.assert_called_once_with(
        "SELECT MIN(timestamp), MAX(timestamp) FROM features WHERE timestamp IS NOT NULL"
    )
    assert suffix_counts.call_args_list == [
        call(connection, "localized_descriptions"),
        call(connection, "localized_names"),
    ]
    assert word_stats.call_args_list == [
        call(connection, localized=False),
        call(connection, localized=True),
    ]
    assert quantile.call_args_list == [
        call(connection, "area_m2", 0.0),
        call(connection, "area_m2", 0.25),
        call(connection, "area_m2", 0.5),
        call(connection, "area_m2", 0.75),
        call(connection, "area_m2", 1.0),
    ]


def test_collect_manifest_summary_accumulates_and_sorts_file_provenance(
    tmp_path: Path,
) -> None:
    first = tmp_path / "b.parquet"
    second = tmp_path / "a.parquet"
    first.write_bytes(b"b" * 5)
    second.write_bytes(b"a" * 3)

    def artifact(
        path: Path, source_name: str, source_size: int, emitted: int, rejections: dict[str, int]
    ):
        manifest = SimpleNamespace(
            source=SimpleNamespace(
                name=source_name, size_bytes=source_size, sha256=f"sha-{source_name}"
            ),
            counts=SimpleNamespace(emitted_features=emitted, rejections=rejections),
        )
        return stats_manifest.ValidatedArtifact(parquet=path, manifest=manifest)

    artifacts = (
        artifact(first, "b.osm.pbf", 20, 3, {"z": 1}),
        artifact(second, "a.osm.pbf", 10, 4, {"a": 2}),
    )

    with (
        patch.object(stats_manifest, "_rows_in_parquet", side_effect=[6, 4]),
        patch.object(stats_manifest, "file_sha256", side_effect=["out-b", "out-a"]),
    ):
        summary = stats_manifest.collect_manifest_summary(artifacts)

    assert summary.emitted_features == 7
    assert summary.rejections == {"a": 2, "z": 1}
    assert summary.source_bytes_total == 30
    assert summary.output_bytes_total == 8
    assert summary.files == [
        {
            "source_pbf": "a.osm.pbf",
            "parquet": "a.parquet",
            "rows": 4,
            "source_bytes": 10,
            "output_bytes": 3,
            "emitted_features": 4,
            "rejections": {"a": 2},
            "source_sha256": "sha-a.osm.pbf",
            "output_sha256": "out-a",
        },
        {
            "source_pbf": "b.osm.pbf",
            "parquet": "b.parquet",
            "rows": 6,
            "source_bytes": 20,
            "output_bytes": 5,
            "emitted_features": 3,
            "rejections": {"z": 1},
            "source_sha256": "sha-b.osm.pbf",
            "output_sha256": "out-b",
        },
    ]


def test_build_stats_payload_preserves_public_fields_and_zero_rate_fallback() -> None:
    feature_summary = stats_features.FeatureSummary(
        rows=10,
        unique_osm_objects=7,
        osm_types={"relation": 4},
        geometry_types={"Polygon": 6},
        description_suffixes={"en": 3},
        name_suffixes={"fr": 2},
        base_description_rows=5,
        localized_description_rows=6,
        base_description_values=5,
        base_description_words_total=20,
        base_description_words_median=4.0,
        localized_description_values=6,
        localized_description_words_total=24,
        localized_description_words_median=4.0,
        base_name_rows=7,
        localized_name_rows=8,
        area_min_m2=1.0,
        area_p25_m2=2.0,
        area_median_m2=3.0,
        area_p75_m2=4.0,
        area_max_m2=5.0,
        data_min_timestamp_utc="2024-01-01T00:00:00+00:00",
        data_max_timestamp_utc="2024-02-01T00:00:00+00:00",
        raw_successful_text_rows=8,
    )
    manifest_summary = stats_manifest.ManifestSummary(
        emitted_features=12,
        rejections={"duplicate_osm_object": 2},
        source_bytes_total=30,
        output_bytes_total=8,
        files=[{"parquet": "a.parquet"}],
    )

    payload = stats_module._build_stats_payload(feature_summary, manifest_summary)

    assert payload["regional_overlap_duplicate_rows"] == 3
    assert payload["regional_overlap_duplicate_rate"] == 0.3
    assert payload["regional_rows"] == 10
    assert payload["regional_rows_with_successful_nonempty_text"] == 8
    assert payload["persisted_text_rejection_rows"] == 2
    assert payload["globally_unique_polygons"] == 7
    assert payload["unique_polygons_with_successful_nonempty_text"] == 10
    assert payload["unique_polygons_with_text"] == 10
    assert payload["deduplicated_rows"] == 3
    assert payload["manifest_duplicate_rows"] == 2
    assert payload["source_bytes_total"] == 30
    assert payload["output_bytes_total"] == 8
    assert payload["area_m2_count"] == 10
    assert payload["area_m2_p25_m2"] == 2.0
    assert payload["area_m2_p75_m2"] == 4.0
    assert payload["files"] == [{"parquet": "a.parquet"}]

    no_duplicate_manifest_summary = stats_manifest.ManifestSummary(
        emitted_features=12,
        rejections={"other": 1},
        source_bytes_total=30,
        output_bytes_total=8,
        files=[{"parquet": "a.parquet"}],
    )
    assert (
        stats_module._build_stats_payload(feature_summary, no_duplicate_manifest_summary)[
            "manifest_duplicate_rows"
        ]
        == 0
    )

    empty_feature_summary = stats_features.FeatureSummary(
        **{**feature_summary.__dict__, "rows": 0, "unique_osm_objects": 0}
    )
    assert (
        stats_module._build_stats_payload(empty_feature_summary, manifest_summary)[
            "regional_overlap_duplicate_rate"
        ]
        == 0.0
    )


def _spatial_batch(rows: list[dict[str, object]]) -> pa.RecordBatch:
    """Build a spatial batch from explicit per-row values."""
    return pa.record_batch(
        [
            pa.array([row["source_pbf"] for row in rows]),
            pa.array(["way"] * len(rows)),
            pa.array([index for index, _ in enumerate(rows)], type=pa.int64()),
            pa.array([row["geometry_type"] for row in rows]),
            pa.array([row.get("area", 1.0) for row in rows], type=pa.float64()),
            pa.array([row["min_x"] for row in rows], type=pa.float64()),
            pa.array([row["min_y"] for row in rows], type=pa.float64()),
            pa.array([row["max_x"] for row in rows], type=pa.float64()),
            pa.array([row["max_y"] for row in rows], type=pa.float64()),
            pa.array([row["wkb"] for row in rows], type=pa.binary()),
        ],
        names=stats_geometry._SPATIAL_COLUMNS,
    )


def _square(x: float, y: float) -> Polygon:
    return Polygon([(x, y), (x, y + 1), (x + 1, y + 1), (x + 1, y)])


def test_the_batch_extent_takes_each_edge_from_its_own_column() -> None:
    """Deliberately asymmetric so swapping a column index cannot go unnoticed."""
    rows = [
        {
            "source_pbf": "region.parquet",
            "geometry_type": "Polygon",
            "min_x": -30.0,
            "min_y": -20.0,
            "max_x": 40.0,
            "max_y": 50.0,
            "wkb": to_wkb(_square(0, 0)),
        },
        {
            "source_pbf": "region.parquet",
            "geometry_type": "Polygon",
            "min_x": -10.0,
            "min_y": -60.0,
            "max_x": 70.0,
            "max_y": 15.0,
            "wkb": to_wkb(_square(2, 2)),
        },
    ]

    summary = stats_geometry._summarize_spatial_batch(
        _spatial_batch(rows), source_name="region.parquet", row_offset=0
    )

    assert summary.dataset_bbox == (-30.0, -60.0, 70.0, 50.0)


def test_multipolygon_components_accumulate_across_the_batch() -> None:
    """Assignment instead of accumulation would report only the final row."""
    multi_two = MultiPolygon([_square(0, 0), _square(5, 5)])
    multi_three = MultiPolygon([_square(0, 0), _square(5, 5), _square(10, 10)])
    rows = [
        {
            "source_pbf": "region.parquet",
            "geometry_type": "MultiPolygon",
            "min_x": 0.0,
            "min_y": 0.0,
            "max_x": 6.0,
            "max_y": 6.0,
            "wkb": to_wkb(multi_two),
        },
        {
            "source_pbf": "region.parquet",
            "geometry_type": "MultiPolygon",
            "min_x": 0.0,
            "min_y": 0.0,
            "max_x": 11.0,
            "max_y": 11.0,
            "wkb": to_wkb(multi_three),
        },
    ]

    summary = stats_geometry._summarize_spatial_batch(
        _spatial_batch(rows), source_name="region.parquet", row_offset=0
    )

    assert summary.multipolygon_components_total == 5


def test_an_invalid_bounding_box_names_its_source_and_row() -> None:
    """The row index is offset by the batch, which is how an operator finds it."""
    rows = [
        {
            "source_pbf": "region.parquet",
            "geometry_type": "Polygon",
            "min_x": float("inf"),
            "min_y": 0.0,
            "max_x": 1.0,
            "max_y": 1.0,
            "wkb": to_wkb(_square(0, 0)),
        }
    ]

    with pytest.raises(
        stats_manifest.ReportingError,
        match=exactly("invalid bounding box in region.parquet at row 7"),
    ):
        stats_geometry._summarize_spatial_batch(
            _spatial_batch(rows), source_name="region.parquet", row_offset=7
        )


def test_merge_bboxes_takes_each_edge_from_its_own_position() -> None:
    """Every coordinate differs, so a swapped index cannot produce the answer."""
    current = (-10.0, -20.0, 30.0, 40.0)
    addition = (-5.0, -25.0, 35.0, 15.0)

    assert stats_geometry._merge_bboxes(current, addition) == (-10.0, -25.0, 35.0, 40.0)
    assert stats_geometry._merge_bboxes(None, addition) == addition
    assert stats_geometry._merge_bboxes(current, None) == current
    assert stats_geometry._merge_bboxes(None, None) is None


def test_spatial_totals_accumulate_across_every_batch(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A single batch cannot tell accumulation from assignment.

    Each counter starts at zero, so with one batch ``total += value`` and
    ``total = value`` agree exactly. Two batches are not enough either when
    only the last one contributes. Three batches, each carrying multipolygon
    parts, separate the two, and the row offset only diverges once a third
    batch has to start after the sum of the first two.
    """

    def _multi(parts: int, *, min_x: float, min_y: float, max_x: float, max_y: float) -> dict:
        squares = [_square(4 * i, 4 * i) for i in range(parts)]
        return {
            "source_pbf": "unique.parquet",
            "geometry_type": "MultiPolygon",
            "min_x": min_x,
            "min_y": min_y,
            "max_x": max_x,
            "max_y": max_y,
            "wkb": to_wkb(MultiPolygon(squares)),
        }

    batches = [
        _spatial_batch([_multi(2, min_x=-10.0, min_y=-20.0, max_x=5.0, max_y=8.0)]),
        _spatial_batch(
            [
                _multi(3, min_x=-3.0, min_y=-40.0, max_x=60.0, max_y=7.0),
                _multi(4, min_x=0.0, min_y=0.0, max_x=1.0, max_y=1.0),
            ]
        ),
        _spatial_batch([_multi(5, min_x=2.0, min_y=3.0, max_x=4.0, max_y=90.0)]),
    ]
    offsets: list[int] = []
    real = stats_geometry._summarize_spatial_batch

    def _record(batch: object, *, source_name: str, row_offset: int):  # type: ignore[no-untyped-def]
        offsets.append(row_offset)
        return real(batch, source_name=source_name, row_offset=row_offset)

    monkeypatch.setattr(stats_geometry, "_summarize_spatial_batch", _record)
    monkeypatch.setattr(
        stats_geometry, "iter_unique_parquet_batches", lambda *_a, **_k: iter(batches)
    )

    data_dir = tmp_path / "data"
    data_dir.mkdir()
    artifact = SimpleNamespace(parquet=data_dir / "unique.parquet")

    summary = stats_geometry.collect_spatial_summary((artifact,))

    # 1 + 2 + 1 rows, so the third batch must start at 3, not at 2.
    assert summary.rows == 4
    assert offsets == [0, 1, 3]
    # 2 + (3 + 4) + 5 parts: assignment would report only the last batch's 5.
    assert summary.multipolygon_components_total == 14
    # every square is one ring, so the rings total tracks the parts total
    assert summary.geometry_rings_total == 14
    # four corners per ring: the closing point is not counted twice
    assert summary.geometry_vertices_total == 14 * 4
    assert summary.dataset_bbox == (-10.0, -40.0, 60.0, 90.0)


def test_hole_counts_accumulate_across_batches(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Holes need their own case: a dataset of solid squares never has any.

    With every batch reporting zero holes, assignment and accumulation agree,
    so the running total is only pinned once two batches each contribute one.
    """
    holed = Polygon(
        [(0, 0), (0, 10), (10, 10), (10, 0)],
        [[(2, 2), (2, 4), (4, 4), (4, 2)]],
    )

    def _row_with_hole(source: str) -> dict:
        return {
            "source_pbf": source,
            "geometry_type": "Polygon",
            "min_x": 0.0,
            "min_y": 0.0,
            "max_x": 10.0,
            "max_y": 10.0,
            "wkb": to_wkb(holed),
        }

    batches = [
        _spatial_batch([_row_with_hole("a.parquet")]),
        _spatial_batch([_row_with_hole("b.parquet")]),
    ]
    monkeypatch.setattr(
        stats_geometry, "iter_unique_parquet_batches", lambda *_a, **_k: iter(batches)
    )

    data_dir = tmp_path / "data"
    data_dir.mkdir()
    summary = stats_geometry.collect_spatial_summary(
        (SimpleNamespace(parquet=data_dir / "unique.parquet"),)
    )

    assert summary.geometry_holes_total == 2
    assert summary.geometry_rings_total == 4


def test_a_row_is_named_by_its_own_source_column_when_present() -> None:
    """Unique-row batches span files, so the row's own source is what names it.

    Falling back to the caller's name here would point an operator at the batch
    rather than at the file the bad row actually came from.
    """
    rows = [
        {
            "source_pbf": "shard-a.parquet",
            "geometry_type": "Polygon",
            "min_x": float("inf"),
            "min_y": 0.0,
            "max_x": 1.0,
            "max_y": 1.0,
            "wkb": to_wkb(_square(0, 0)),
        }
    ]

    with pytest.raises(
        stats_manifest.ReportingError,
        match=exactly("invalid bounding box in shard-a.parquet at row 3"),
    ):
        stats_geometry._summarize_spatial_batch(
            _spatial_batch(rows), source_name="region.parquet", row_offset=3
        )


def _good_row(source: str) -> dict[str, object]:
    return {
        "source_pbf": source,
        "geometry_type": "Polygon",
        "min_x": 0.0,
        "min_y": 0.0,
        "max_x": 1.0,
        "max_y": 1.0,
        "wkb": to_wkb(_square(0, 0)),
    }


@pytest.mark.parametrize(
    ("broken", "message"),
    [
        pytest.param({"area": float("nan")}, "invalid area in", id="area"),
        pytest.param({"geometry_type": None}, "missing geometry type in", id="geometry-type"),
        pytest.param(
            {"geometry_type": "MultiPolygon"},
            "invalid geometry in",
            id="geometry-measurement",
        ),
    ],
)
def test_every_row_validator_is_told_which_row_it_is_looking_at(
    broken: dict[str, object], message: str
) -> None:
    """Each validator names the failing row's own file and absolute index.

    The bad row is the second of the batch and the batch starts at row 3, so an
    index that ignored the offset, or subtracted it, would name row 1 or row 2.
    Its source column differs from the caller's name, so a validator handed the
    batch name instead of the row's would name the wrong file.
    """
    rows = [_good_row("shard-a.parquet"), _good_row("shard-b.parquet") | broken]

    with pytest.raises(
        stats_manifest.ReportingError,
        match=rf"\A{re.escape(message)} shard-b\.parquet at row 4(:|\Z)",
    ):
        stats_geometry._summarize_spatial_batch(
            _spatial_batch(rows), source_name="region.parquet", row_offset=3
        )


def test_hole_counts_accumulate_across_rows_of_one_batch() -> None:
    """Within a batch too, each row adds to the running total.

    The cross-batch test cannot see this: it uses one row per batch, so
    assigning and adding agree there. Here the last row has fewer holes than
    the batch as a whole, so assignment reports the last row's count.
    """
    two_holes = Polygon(
        [(0, 0), (0, 10), (10, 10), (10, 0)],
        [[(1, 1), (1, 2), (2, 2), (2, 1)], [(5, 5), (5, 6), (6, 6), (6, 5)]],
    )
    one_hole = Polygon(
        [(0, 0), (0, 10), (10, 10), (10, 0)],
        [[(3, 3), (3, 4), (4, 4), (4, 3)]],
    )
    rows = [
        _good_row("region.parquet") | {"wkb": to_wkb(geometry)}
        for geometry in (two_holes, one_hole)
    ]

    summary = stats_geometry._summarize_spatial_batch(
        _spatial_batch(rows), source_name="region.parquet", row_offset=0
    )

    assert summary.geometry_holes_total == 3


def test_a_batch_without_a_source_column_falls_back_to_the_given_name() -> None:
    """A per-file batch has no source column, so the caller's name is used."""
    batch = pa.record_batch(
        [
            pa.array(["way"]),
            pa.array([1], type=pa.int64()),
            pa.array(["Polygon"]),
            pa.array([1.0], type=pa.float64()),
            pa.array([float("inf")], type=pa.float64()),
            pa.array([0.0], type=pa.float64()),
            pa.array([1.0], type=pa.float64()),
            pa.array([1.0], type=pa.float64()),
            pa.array([to_wkb(_square(0, 0))], type=pa.binary()),
        ],
        names=[name for name in stats_geometry._SPATIAL_COLUMNS if name != "source_pbf"],
    )

    with pytest.raises(
        stats_manifest.ReportingError,
        match=exactly("invalid bounding box in region.parquet at row 3"),
    ):
        stats_geometry._summarize_spatial_batch(batch, source_name="region.parquet", row_offset=3)


def test_a_malformed_geometry_names_its_source_and_row() -> None:
    """The row's identity travels down into every measurement failure."""
    with pytest.raises(
        stats_manifest.ReportingError,
        match=r"\Amalformed geometry in region\.parquet at row 9: .*\Z",
    ):
        stats_geometry._geometry_measurements(
            b"not wkb", "Polygon", source_name="region.parquet", row_index=9
        )


def test_an_unsupported_geometry_type_names_its_source_and_row() -> None:
    with pytest.raises(
        stats_manifest.ReportingError,
        match=exactly("unsupported geometry in region.parquet at row 4: 'Point'"),
    ):
        stats_geometry._geometry_measurements(
            to_wkb(Point(0, 0)), "Point", source_name="region.parquet", row_index=4
        )


def test_holes_accumulate_across_polygon_components() -> None:
    """Assignment would report only the last component's holes.

    Every existing fixture used solid squares, where each component reports
    zero and the two forms agree.
    """
    holed = Polygon(
        [(0, 0), (0, 10), (10, 10), (10, 0)],
        [[(1, 1), (1, 2), (2, 2), (2, 1)], [(4, 4), (4, 5), (5, 5), (5, 4)]],
    )
    other = Polygon(
        [(20, 20), (20, 30), (30, 30), (30, 20)], [[(21, 21), (21, 22), (22, 22), (22, 21)]]
    )

    vertices, rings, holes = stats_geometry._polygon_measurements((holed, other))

    assert holes == 3
    assert rings == 5
    assert vertices == 20
