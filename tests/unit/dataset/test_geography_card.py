"""Dataset-card integration tests for the H3 density map.

These tests prove that:

* the map block is added exactly once;
* the relative asset reference is correct for the Hugging Face repository;
* regenerating the card with unchanged map bytes does not modify the
  surrounding prose (byte-level regression);
* existing generated stats remain unchanged by the map addition;
* repeated regeneration does not insert a duplicate block.
"""

from __future__ import annotations

import re
from pathlib import Path
from unittest.mock import Mock, patch

import pytest
from shapely.geometry import Polygon

import osm_polygon_description_tag.dataset.geography.card as card_module
from osm_polygon_description_tag._resources import dataset_card_template
from osm_polygon_description_tag.dataset.geography import (
    H3_MAP_ASSET_RELATIVE_PATH,
    H3_MAP_END_MARKER,
    H3_MAP_START_MARKER,
    H3_MAP_TITLE,
    install_map_block,
    render_map_block,
)
from osm_polygon_description_tag.dataset.geography.card import (
    _atomic_write_template,
    _template_with_map_markers,
    _validate_marker_counts,
    write_map_block_marker_to_template,
)
from osm_polygon_description_tag.dataset.reporting import generate_dataset_docs
from osm_polygon_description_tag.workflow.orchestrator import _build_metadata_only_upload_plan
from tests.conftest import make_record_dict
from tests.helpers.dataset import write_finalized_dataset


def _populate_dataset(data_root: Path, source_root: Path) -> None:
    shards = {
        stem: [
            make_record_dict(
                Polygon([(0, 0), (0, 1), (1, 1), (1, 0)]),
                {"description": stem},
                osm_id=osm_id,
                source_pbf=f"{stem}.osm.pbf",
            )
        ]
        for stem, osm_id in [("alpha", 1), ("beta", 2)]
    }
    write_finalized_dataset(data_root, source_root, shards)


def _stub_map_block(stats_sha256: str, total_rows: int, occupied_cells: int) -> str:
    """Return a deterministic map body (no markers) for unit tests."""
    return f"![H3 density of description-tagged polygons]({H3_MAP_ASSET_RELATIVE_PATH})\n"


# ---------------------------------------------------------------------------
# Marker contract
# ---------------------------------------------------------------------------


def test_install_map_block_inserts_single_block() -> None:
    template = "before\n\n<!-- GENERATED:H3_MAP:START -->\n<!-- GENERATED:H3_MAP:END -->\n\nafter"
    out = install_map_block(template, "![alt](path.png)")
    assert out.count(H3_MAP_START_MARKER) == 1
    assert out.count(H3_MAP_END_MARKER) == 1


def test_install_map_block_is_idempotent() -> None:
    template = "before\n\n<!-- GENERATED:H3_MAP:START -->\n<!-- GENERATED:H3_MAP:END -->\n\nafter"
    once = install_map_block(template, "![alt](path.png)")
    twice = install_map_block(once, "![alt](path.png)")
    assert once == twice
    assert twice.count(H3_MAP_START_MARKER) == 1

    # Calling with a different body replaces the body but does not duplicate.
    thrice = install_map_block(once, "![alt2](path2.png)")
    assert thrice.count(H3_MAP_START_MARKER) == 1
    assert "path2.png" in thrice


def test_install_map_block_preserves_surrounding_text() -> None:
    template = (
        "header\n"
        "\n"
        "<!-- GENERATED:STATS:START -->\n"
        "stats content\n"
        "<!-- GENERATED:STATS:END -->\n"
        "\n"
        "## Methods\n"
        "\n"
        "Some prose.\n"
        "\n"
        "<!-- GENERATED:H3_MAP:START -->\n"
        "<!-- GENERATED:H3_MAP:END -->\n"
        "\n"
        "footer\n"
    )
    out = install_map_block(template, "![alt](map.png)")
    # Everything outside the map block must be byte-identical.
    start = out.index(H3_MAP_START_MARKER)
    end = out.index(H3_MAP_END_MARKER) + len(H3_MAP_END_MARKER)
    outside = out[:start] + out[end:]
    outside_template = (
        template[: template.index(H3_MAP_START_MARKER)]
        + template[template.index(H3_MAP_END_MARKER) + len(H3_MAP_END_MARKER) :]
    )
    assert outside == outside_template


def test_install_map_block_rejects_template_without_markers() -> None:
    template = "no markers here"
    with pytest.raises(ValueError, match="markers"):
        install_map_block(template, "![alt](map.png)")


def test_install_map_block_rejects_duplicate_markers() -> None:
    template = (
        "<!-- GENERATED:H3_MAP:START -->\n"
        "<!-- GENERATED:H3_MAP:END -->\n"
        "<!-- GENERATED:H3_MAP:START -->\n"
        "<!-- GENERATED:H3_MAP:END -->\n"
    )
    with pytest.raises(ValueError, match="unique"):
        install_map_block(template, "![alt](map.png)")


def test_render_map_block_uses_relative_asset_path() -> None:
    block = render_map_block()
    assert H3_MAP_ASSET_RELATIVE_PATH in block
    assert H3_MAP_START_MARKER in block
    assert H3_MAP_END_MARKER in block


def test_h3_map_title_is_set() -> None:
    assert H3_MAP_TITLE
    assert "H3" in H3_MAP_TITLE


# ---------------------------------------------------------------------------
# End-to-end card integration
# ---------------------------------------------------------------------------


def test_generation_installs_map_block_with_correct_relative_path(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The dataset card template carries the H3 map block at the right place."""
    data_root = tmp_path / "generated"
    source_root = tmp_path / "raw"
    _populate_dataset(data_root, source_root)

    # Patch the rendering helper used inside generate_dataset_docs to a
    # deterministic stub so the test does not require matplotlib.
    monkeypatch.setattr(
        "osm_polygon_description_tag.dataset.docs._render_h3_map_block",
        lambda data_root, total_rows, occupied_cells: _stub_map_block(
            "deadbeef", total_rows, occupied_cells
        ),
        raising=False,
    )
    # Patch the dataset.reporting module to also use a stub for the PNG.
    monkeypatch.setattr(
        "osm_polygon_description_tag.dataset.docs._write_h3_map_png",
        lambda data_root, total_rows, occupied_cells, counts=None: None,
        raising=False,
    )

    generate_dataset_docs(
        data_root, dataset_card_template(), clock=lambda: "2026-01-01T00:00:00+00:00"
    )
    readme = (data_root / "README.md").read_text(encoding="utf-8")
    assert readme.count(H3_MAP_START_MARKER) == 1
    assert readme.count(H3_MAP_END_MARKER) == 1
    assert H3_MAP_ASSET_RELATIVE_PATH in readme
    # The map asset path is relative to the dataset repository root.
    assert not H3_MAP_ASSET_RELATIVE_PATH.startswith("/")


def test_generation_aggregates_h3_once_and_reuses_counts(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The card and PNG must share one bounded H3 aggregation pass."""
    data_root = tmp_path / "generated"
    source_root = tmp_path / "raw"
    _populate_dataset(data_root, source_root)

    import osm_polygon_description_tag.dataset.docs as reporting

    calls: list[Path] = []
    captured: dict[str, object] = {}

    def fake_aggregate(root: Path) -> dict[str, int]:
        calls.append(root)
        return {"85280003fffffff": 2}

    def fake_render(counts: dict[str, int], output_path: Path) -> None:
        captured["counts"] = counts
        output_path.parent.mkdir(parents=True, exist_ok=True)
        output_path.write_bytes(b"deterministic-map")

    monkeypatch.setattr(reporting, "aggregate_h3_density", fake_aggregate)
    monkeypatch.setattr(reporting, "render_density_map", fake_render)

    reporting.generate_dataset_docs(
        data_root, dataset_card_template(), clock=lambda: "2026-01-01T00:00:00+00:00"
    )

    assert calls == [data_root]
    assert captured["counts"] == {"85280003fffffff": 2}


def test_readme_only_regeneration_reuses_existing_h3_map(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Changing the card template does not reaggregate or rewrite the map."""
    data_root = tmp_path / "generated"
    source_root = tmp_path / "raw"
    _populate_dataset(data_root, source_root)

    import osm_polygon_description_tag.dataset.docs as reporting

    aggregate_calls: list[Path] = []

    def fake_aggregate(root: Path) -> dict[str, int]:
        aggregate_calls.append(root)
        return {"85280003fffffff": 2}

    def fake_render(_counts: dict[str, int], output_path: Path) -> None:
        output_path.parent.mkdir(parents=True, exist_ok=True)
        output_path.write_bytes(b"stable-map")

    monkeypatch.setattr(reporting, "aggregate_h3_density", fake_aggregate)
    monkeypatch.setattr(reporting, "render_density_map", fake_render)

    template = dataset_card_template()
    reporting.generate_dataset_docs(data_root, template)
    map_path = data_root / H3_MAP_ASSET_RELATIVE_PATH
    first_map_bytes = map_path.read_bytes()
    first_map_mtime = map_path.stat().st_mtime_ns

    changed_template = tmp_path / "changed-template.md"
    changed_template.write_text(
        template.read_text(encoding="utf-8") + "\n<!-- editorial prose changed -->\n",
        encoding="utf-8",
    )
    reporting.generate_dataset_docs(data_root, changed_template)

    assert aggregate_calls == [data_root]
    assert map_path.read_bytes() == first_map_bytes
    assert map_path.stat().st_mtime_ns == first_map_mtime
    assert "editorial prose changed" in (data_root / "README.md").read_text(encoding="utf-8")


def test_generation_preserves_existing_stats_block(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The map addition must not perturb the existing stats block."""
    data_root = tmp_path / "generated"
    source_root = tmp_path / "raw"
    _populate_dataset(data_root, source_root)

    monkeypatch.setattr(
        "osm_polygon_description_tag.dataset.docs._render_h3_map_block",
        lambda data_root, total_rows, occupied_cells: _stub_map_block(
            "deadbeef", total_rows, occupied_cells
        ),
        raising=False,
    )
    monkeypatch.setattr(
        "osm_polygon_description_tag.dataset.docs._write_h3_map_png",
        lambda data_root, total_rows, occupied_cells, counts=None: None,
        raising=False,
    )

    generate_dataset_docs(data_root, dataset_card_template())
    readme = (data_root / "README.md").read_text(encoding="utf-8")

    start_stats = readme.index("<!-- GENERATED:STATS:START -->")
    end_stats = readme.index("<!-- GENERATED:STATS:END -->") + len("<!-- GENERATED:STATS:END -->")
    start_map = readme.index("<!-- GENERATED:H3_MAP:START -->")
    end_map = readme.index("<!-- GENERATED:H3_MAP:END -->") + len("<!-- GENERATED:H3_MAP:END -->")

    # The two generated blocks must be independent and complete. The H3 map
    # block sits above the stats block so the visual asset is visible
    # before the tabular summary.
    assert start_map < start_stats
    stats_block = readme[start_stats:end_stats]
    map_block = readme[start_map:end_map]
    assert "## Dataset at a glance" in stats_block
    assert H3_MAP_ASSET_RELATIVE_PATH in map_block


def test_byte_level_regression_only_map_block_changes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Regenerating the card changes only the map block; everything else is verbatim."""
    data_root = tmp_path / "generated"
    source_root = tmp_path / "raw"
    _populate_dataset(data_root, source_root)
    template = dataset_card_template()

    # First generation: install the map block with text A.
    monkeypatch.setattr(
        "osm_polygon_description_tag.dataset.docs._render_h3_map_block",
        lambda data_root, total_rows, occupied_cells: "![alt version A](map.png)\n",
        raising=False,
    )
    monkeypatch.setattr(
        "osm_polygon_description_tag.dataset.docs._write_h3_map_png",
        lambda data_root, total_rows, occupied_cells, counts=None: None,
        raising=False,
    )
    generate_dataset_docs(data_root, template)
    readme_a = (data_root / "README.md").read_text(encoding="utf-8")

    # Second generation: same surrounding prose, different map text.
    monkeypatch.setattr(
        "osm_polygon_description_tag.dataset.docs._render_h3_map_block",
        lambda data_root, total_rows, occupied_cells: "![alt version B](map.png)\n",
        raising=False,
    )
    generate_dataset_docs(data_root, template)
    readme_b = (data_root / "README.md").read_text(encoding="utf-8")

    # Extract the area outside the map block; it must be identical.
    a_start = readme_a.index(H3_MAP_START_MARKER)
    a_end = readme_a.index(H3_MAP_END_MARKER) + len(H3_MAP_END_MARKER)
    b_start = readme_b.index(H3_MAP_START_MARKER)
    b_end = readme_b.index(H3_MAP_END_MARKER) + len(H3_MAP_END_MARKER)
    outside_a = readme_a[:a_start] + readme_a[a_end:]
    outside_b = readme_b[:b_start] + readme_b[b_end:]
    assert outside_a == outside_b


def test_idempotent_regeneration_does_not_duplicate_map_block(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Repeated regeneration never inserts a second map block."""
    data_root = tmp_path / "generated"
    source_root = tmp_path / "raw"
    _populate_dataset(data_root, source_root)

    monkeypatch.setattr(
        "osm_polygon_description_tag.dataset.docs._render_h3_map_block",
        lambda data_root, total_rows, occupied_cells: _stub_map_block(
            "deadbeef", total_rows, occupied_cells
        ),
        raising=False,
    )
    monkeypatch.setattr(
        "osm_polygon_description_tag.dataset.docs._write_h3_map_png",
        lambda data_root, total_rows, occupied_cells, counts=None: None,
        raising=False,
    )
    for _ in range(3):
        generate_dataset_docs(data_root, dataset_card_template())
    readme = (data_root / "README.md").read_text(encoding="utf-8")
    assert readme.count(H3_MAP_START_MARKER) == 1
    assert readme.count(H3_MAP_END_MARKER) == 1


def test_metadata_only_plan_includes_map_when_present(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The metadata plan contains both visual assets when they exist."""
    data_root = tmp_path / "generated"
    source_root = tmp_path / "raw"
    _populate_dataset(data_root, source_root)
    (data_root / "README.md").write_text("# README")
    (data_root / "stats.json").write_text("{}")
    (data_root / "assets").mkdir()
    (data_root / "assets" / "description_polygon_density.png").write_bytes(b"\x89PNG\r\n\x1a\n")
    (data_root / "assets" / "area_distribution.png").write_bytes(b"\x89PNG\r\n\x1a\n")
    (data_root / "assets" / "dataset-card-hero.png").write_bytes(b"\x89PNG\r\n\x1a\n")
    plan = _build_metadata_only_upload_plan(data_root)
    relative = sorted(item.relative_path for item in plan.files)
    assert "assets/description_polygon_density.png" in relative
    assert "assets/area_distribution.png" in relative
    assert "assets/dataset-card-hero.png" in relative


# ---------------------------------------------------------------------------
# Pre-feature README byte contract regression test
# ---------------------------------------------------------------------------


_PRE_H3_TEMPLATE_PATH = Path("tests/fixtures/dataset_card_template_pre_h3.md")
_H3_MAP_BLOCK_PATTERN = re.compile(
    r"<!-- GENERATED:H3_MAP:START -->[^\n]*\n.*?<!-- GENERATED:H3_MAP:END -->\n",
    re.DOTALL,
)


def _first_diff(left: str, right: str) -> str:
    """Return the first character-level difference between two strings."""
    for index in range(min(len(left), len(right))):
        if left[index] != right[index]:
            return (
                f"index={index} left={left[max(0, index - 20) : index + 30]!r} "
                f"right={right[max(0, index - 20) : index + 30]!r}"
            )
    return f"lengths differ: left={len(left)} right={len(right)}"


@pytest.mark.parametrize(
    "template_path",
    (
        Path("docs/dataset-card-template.md"),
        Path("src/osm_polygon_description_tag/_data/dataset-card-template.md"),
    ),
)
def test_template_minus_h3_map_block_matches_pre_h3_template(template_path: Path) -> None:
    """Removing the H3 map marker block restores the pre-feature template byte-for-byte.

    The public-facing dataset card template may differ from the pre-feature
    version only by the addition of the H3 map marker block and the canonical
    image reference it carries. This regression test proves that every other
    byte in the template (prose, headings, stats/methodology/limitations,
    license, reproducibility, whitespace) is identical to the pre-feature
    template that was in place before the H3 map feature was introduced.
    """
    current = template_path.read_text(encoding="utf-8")
    pre_h3 = _PRE_H3_TEMPLATE_PATH.read_text(encoding="utf-8")

    # Sanity check: the current template must contain the H3 marker block;
    # otherwise the regression guard is meaningless.
    assert H3_MAP_START_MARKER in current
    assert H3_MAP_END_MARKER in current
    # The pre-feature template must NOT contain the H3 marker block.
    assert H3_MAP_START_MARKER not in pre_h3
    assert H3_MAP_END_MARKER not in pre_h3

    # Strip the H3 marker block (start marker, image reference, end marker)
    # from the current template. The marker block in the current template
    # is the only post-feature addition; the surrounding whitespace is
    # structured so the pre-feature separator slot is restored on strip.
    without_map = _H3_MAP_BLOCK_PATTERN.sub("", current, count=1)
    assert without_map == pre_h3, (
        "Removing the H3 map marker block must restore the pre-feature "
        f"template byte-for-byte. Differences are: "
        f"{set(without_map.splitlines()) ^ set(pre_h3.splitlines())}"
    )


def test_generated_readme_preserves_surrounding_prose_by_stripping_markers(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The generated README equals the pre-feature template outside both generated markers.

    The generation pipeline rewrites the canonical stats block and
    installs the H3 map marker block, but every byte outside those two
    generated regions must remain identical to the pre-feature template.
    Stripping both generated marker blocks from the produced README must
    yield exactly the pre-feature template with the same marker blocks
    stripped.
    """
    data_root = tmp_path / "generated"
    source_root = tmp_path / "raw"
    _populate_dataset(data_root, source_root)

    # Stub the PNG writer because matplotlib is not the contract under test.
    monkeypatch.setattr(
        "osm_polygon_description_tag.dataset.docs._write_h3_map_png",
        lambda data_root, total_rows, occupied_cells, counts=None: None,
        raising=False,
    )

    generate_dataset_docs(data_root, dataset_card_template())
    readme = (data_root / "README.md").read_text(encoding="utf-8")
    pre_h3 = _PRE_H3_TEMPLATE_PATH.read_text(encoding="utf-8")

    # Sanity: the generated README carries both generated blocks.
    assert "<!-- GENERATED:STATS:START -->" in readme
    assert "<!-- GENERATED:H3_MAP:START -->" in readme
    assert H3_MAP_ASSET_RELATIVE_PATH in readme

    # Strip both generated marker blocks (same regex on both sides).
    stats_marker_pattern = re.compile(
        r"<!-- GENERATED:STATS:START -->.*?<!-- GENERATED:STATS:END -->\n",
        re.DOTALL,
    )

    def _strip(text: str) -> str:
        text = stats_marker_pattern.sub("", text, count=1)
        text = _H3_MAP_BLOCK_PATTERN.sub("", text, count=1)
        return text

    stripped_readme = _strip(readme)
    stripped_pre_h3 = _strip(pre_h3)
    assert stripped_readme == stripped_pre_h3, (
        "Outside the stats and H3 map generated markers, the generated README "
        "must be byte-identical to the pre-feature template. First diff: "
        f"{_first_diff(stripped_readme, stripped_pre_h3)}"
    )


# ---------------------------------------------------------------------------
# Template marker ordering, synchronization, and idempotency
# ---------------------------------------------------------------------------


def _pre_h3_template_text() -> str:
    """Return the pre-feature dataset card template stripped of its H3 marker block."""
    return _H3_MAP_BLOCK_PATTERN.sub(
        "", Path("docs/dataset-card-template.md").read_text(encoding="utf-8"), count=1
    )


def test_both_markers_occur_exactly_once_in_each_template() -> None:
    """The packaged and documentation templates each contain exactly one map and stats block."""
    for template_path in (
        Path("docs/dataset-card-template.md"),
        Path("src/osm_polygon_description_tag/_data/dataset-card-template.md"),
    ):
        text = template_path.read_text(encoding="utf-8")
        assert text.count(H3_MAP_START_MARKER) == 1, template_path
        assert text.count(H3_MAP_END_MARKER) == 1, template_path
        assert text.count("<!-- GENERATED:STATS:START -->") == 1, template_path
        assert text.count("<!-- GENERATED:STATS:END -->") == 1, template_path


def test_map_marker_appears_before_stats_marker_in_each_template() -> None:
    """The H3 map marker must sit immediately above the stats marker in every template."""
    for template_path in (
        Path("docs/dataset-card-template.md"),
        Path("src/osm_polygon_description_tag/_data/dataset-card-template.md"),
    ):
        text = template_path.read_text(encoding="utf-8")
        start_map = text.index(H3_MAP_START_MARKER)
        end_map = text.index(H3_MAP_END_MARKER) + len(H3_MAP_END_MARKER)
        start_stats = text.index("<!-- GENERATED:STATS:START -->")
        assert start_map < start_stats, template_path
        # The map marker block ends before the stats marker begins.
        assert end_map <= start_stats, template_path


def test_packaged_and_documentation_templates_remain_synchronized() -> None:
    """The packaged and documentation templates are byte-for-byte identical."""
    packaged = Path("src/osm_polygon_description_tag/_data/dataset-card-template.md").read_text(
        encoding="utf-8"
    )
    documented = Path("docs/dataset-card-template.md").read_text(encoding="utf-8")
    assert packaged == documented


def test_write_map_block_marker_inserts_single_block_before_stats(
    tmp_path: Path,
) -> None:
    """``write_map_block_marker_to_template`` injects exactly one map block above stats."""
    template = tmp_path / "template.md"
    template.write_text(_pre_h3_template_text(), encoding="utf-8")
    write_map_block_marker_to_template(template)

    text = template.read_text(encoding="utf-8")
    assert text.count(H3_MAP_START_MARKER) == 1
    assert text.count(H3_MAP_END_MARKER) == 1
    assert H3_MAP_ASSET_RELATIVE_PATH in text
    start_map = text.index(H3_MAP_START_MARKER)
    start_stats = text.index("<!-- GENERATED:STATS:START -->")
    assert start_map < start_stats


def test_write_map_block_marker_preserves_surrounding_prose(tmp_path: Path) -> None:
    """Outside the map marker block, the surrounding prose is preserved byte-for-byte."""
    original = _pre_h3_template_text()
    template = tmp_path / "template.md"
    template.write_text(original, encoding="utf-8")
    write_map_block_marker_to_template(template)
    text = template.read_text(encoding="utf-8")

    start = text.index(H3_MAP_START_MARKER)
    end = text.index(H3_MAP_END_MARKER) + len(H3_MAP_END_MARKER)
    # The expected text equals the pre-feature template with the H3 block
    # inserted immediately before the stats marker.
    expected = (
        original[: original.index("<!-- GENERATED:STATS:START -->")] + text[start:end] + text[end:]
    )
    assert text == expected
    # Stripping the H3 marker block (start marker, image reference, end
    # marker, and its trailing newline) must restore the pre-feature template
    # byte-for-byte.
    outside = _H3_MAP_BLOCK_PATTERN.sub("", text, count=1)
    assert outside == original


def test_write_map_block_marker_is_idempotent(tmp_path: Path) -> None:
    """Repeated calls never duplicate the map block."""
    template = tmp_path / "template.md"
    template.write_text(_pre_h3_template_text(), encoding="utf-8")
    write_map_block_marker_to_template(template)
    once = template.read_text(encoding="utf-8")
    write_map_block_marker_to_template(template)
    twice = template.read_text(encoding="utf-8")
    assert once == twice
    assert twice.count(H3_MAP_START_MARKER) == 1
    assert twice.count(H3_MAP_END_MARKER) == 1


def test_write_map_block_marker_is_noop_when_markers_present(tmp_path: Path) -> None:
    """When the markers are already present, the writer is a no-op (mtime preserved)."""
    template = tmp_path / "template.md"
    template.write_text(dataset_card_template().read_text(encoding="utf-8"), encoding="utf-8")
    original_mtime = template.stat().st_mtime_ns
    write_map_block_marker_to_template(template)
    assert template.stat().st_mtime_ns == original_mtime


def test_write_map_block_marker_rejects_duplicate_markers(tmp_path: Path) -> None:
    """A template with duplicate map markers must be rejected."""
    template = tmp_path / "template.md"
    base = dataset_card_template().read_text(encoding="utf-8")
    duplicate = base.replace(H3_MAP_END_MARKER, f"{H3_MAP_END_MARKER}\n{H3_MAP_START_MARKER}", 1)
    duplicate = duplicate.replace(
        "<!-- GENERATED:STATS:START -->",
        f"{H3_MAP_END_MARKER}\n<!-- GENERATED:STATS:START -->",
        1,
    )
    assert duplicate.count(H3_MAP_START_MARKER) == 2
    assert duplicate.count(H3_MAP_END_MARKER) == 2
    template.write_text(duplicate, encoding="utf-8")
    with pytest.raises(ValueError, match="unique"):
        write_map_block_marker_to_template(template)


def test_write_map_block_marker_rejects_template_without_stats_marker(tmp_path: Path) -> None:
    """A template missing the stats marker cannot host the map marker."""
    template = tmp_path / "template.md"
    template.write_text("# orphan\n\nno stats marker here\n", encoding="utf-8")
    with pytest.raises(ValueError, match="STATS:START"):
        write_map_block_marker_to_template(template)


@pytest.mark.parametrize("start_count, end_count", [(2, 1), (1, 2), (2, 2)])
def test_validate_marker_counts_rejects_each_duplicate_marker_side(
    start_count: int, end_count: int
) -> None:
    with pytest.raises(ValueError) as error:
        _validate_marker_counts(start_count, end_count)

    assert str(error.value) == (
        "dataset card template must contain a unique H3 map marker block; "
        f"found {start_count} start markers and {end_count} end markers"
    )


def test_write_map_block_marker_requires_both_markers_before_noop() -> None:
    template = Mock()
    text = f"{H3_MAP_START_MARKER}\n"
    template.read_text.return_value = text

    with (
        patch.object(card_module, "_template_with_map_markers", return_value="new") as build,
        patch.object(card_module, "_atomic_write_template") as write,
    ):
        write_map_block_marker_to_template(template, asset_relative_path="asset.png")

    template.read_text.assert_called_once_with(encoding="utf-8")
    build.assert_called_once_with(text, "asset.png")
    write.assert_called_once_with(template, "new")


def test_template_with_map_markers_replaces_only_the_first_stats_marker() -> None:
    stats_marker = "<!-- GENERATED:STATS:START -->\n"
    text = f"before\n{stats_marker}first\n{stats_marker}second\n"

    output = _template_with_map_markers(text, "assets/map.png")

    assert output.count(H3_MAP_START_MARKER) == 1
    assert output.count(stats_marker) == 2
    assert output.endswith(f"{stats_marker}second\n")


def test_atomic_write_template_uses_explicit_utf8_and_binary_fsync_open() -> None:
    template = Mock()
    temporary = Mock()
    temporary.exists.return_value = False
    template.with_name.return_value = temporary
    handle = Mock()
    handle.__enter__ = Mock(return_value=handle)
    handle.__exit__ = Mock(return_value=None)

    with (
        patch("builtins.open", return_value=handle) as open_file,
        patch.object(card_module.os, "fsync") as fsync,
        patch.object(card_module.os, "replace") as replace,
    ):
        _atomic_write_template(template, "new text")

    temporary.write_text.assert_called_once_with("new text", encoding="utf-8")
    open_file.assert_called_once_with(temporary, "rb")
    fsync.assert_called_once_with(handle.fileno())
    replace.assert_called_once_with(temporary, template)


# ---------------------------------------------------------------------------
# Map cache identity: reuse when stable, recompute when finalized data changes
# ---------------------------------------------------------------------------


def _fake_record(stem: str, osm_id: int = 1) -> dict:
    """Return a single description-tagged polygon record used to populate fixtures."""
    from tests.conftest import make_record_dict

    return make_record_dict(
        Polygon([(0, 0), (0, 1), (1, 1), (1, 0)]),
        {"description": stem},
        osm_id=osm_id,
        source_pbf=f"{stem}.osm.pbf",
    )


def test_readme_only_regeneration_does_not_rewrite_h3_png_when_cache_identity_unchanged(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A second ``generate_dataset_docs`` call with the same data is a no-op for the PNG."""
    data_root = tmp_path / "generated"
    source_root = tmp_path / "raw"
    _populate_dataset(data_root, source_root)

    import osm_polygon_description_tag.dataset.docs as reporting

    aggregate_calls: list[Path] = []
    render_calls: list[Path] = []

    def fake_aggregate(root: Path) -> dict[str, int]:
        aggregate_calls.append(root)
        return {"85280003fffffff": 2}

    def fake_render(_counts: dict[str, int], output_path: Path) -> None:
        render_calls.append(output_path)
        output_path.parent.mkdir(parents=True, exist_ok=True)
        output_path.write_bytes(b"stable-map")

    monkeypatch.setattr(reporting, "aggregate_h3_density", fake_aggregate)
    monkeypatch.setattr(reporting, "render_density_map", fake_render)

    template = dataset_card_template()
    reporting.generate_dataset_docs(data_root, template)
    map_path = data_root / H3_MAP_ASSET_RELATIVE_PATH
    map_bytes = map_path.read_bytes()
    map_mtime = map_path.stat().st_mtime_ns

    # Second call: same template, identical finalized Parquets. The PNG must
    # not be reaggregated or rewritten.
    reporting.generate_dataset_docs(data_root, template)

    assert aggregate_calls == [data_root], "H3 aggregation must not repeat"
    assert render_calls == [map_path], "H3 PNG must not be re-rendered"
    assert map_path.read_bytes() == map_bytes
    assert map_path.stat().st_mtime_ns == map_mtime


def test_map_is_recomputed_when_finalized_parquet_data_changes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A change to the finalized Parquet bytes invalidates the map cache."""
    data_root = tmp_path / "generated"
    source_root = tmp_path / "raw"
    _populate_dataset(data_root, source_root)

    import osm_polygon_description_tag.dataset.docs as reporting

    aggregate_calls: list[Path] = []
    render_calls: list[Path] = []
    identity_iter = iter(["identity-v1", "identity-v2"])

    def fake_aggregate(root: Path) -> dict[str, int]:
        aggregate_calls.append(root)
        return {"85280003fffffff": 2}

    def fake_render(_counts: dict[str, int], output_path: Path) -> None:
        render_calls.append(output_path)
        output_path.parent.mkdir(parents=True, exist_ok=True)
        output_path.write_bytes(f"map-{len(render_calls)}".encode())

    def fake_h3_map_input_sha256(stats: object) -> str:
        return next(identity_iter)

    monkeypatch.setattr(reporting, "aggregate_h3_density", fake_aggregate)
    monkeypatch.setattr(reporting, "render_density_map", fake_render)
    monkeypatch.setattr(reporting, "_h3_map_input_sha256", fake_h3_map_input_sha256)

    template = dataset_card_template()
    reporting.generate_dataset_docs(data_root, template)
    map_path = data_root / H3_MAP_ASSET_RELATIVE_PATH
    first_bytes = map_path.read_bytes()
    assert first_bytes == b"map-1"

    # Simulate finalized Parquet bytes changing: the next identity is different
    # from the cached identity, so the PNG must be regenerated.
    reporting.generate_dataset_docs(data_root, template)

    assert aggregate_calls == [data_root, data_root], "H3 aggregation must rerun on data change"
    assert len(render_calls) == 2, "H3 PNG must be re-rendered on data change"
    assert map_path.read_bytes() == b"map-2"
    assert map_path.read_bytes() != first_bytes
