# OSM Polygon Description Dataset Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Build a public, reproducible, resumable pipeline that creates one validated GeoParquet file per OSM PBF for polygons carrying `description` or `description:<suffix>` tags.

**Architecture:** Stream versioned OSM area output from `osmium export` in PostgreSQL COPY format into focused Python modules that transform bounded batches, atomically write GeoParquet, validate manifests, aggregate factual statistics, and generate the Hugging Face dataset card. Keep code, immutable raw PBFs, generated data, and publication as separate trust boundaries.

**Tech Stack:** Python 3.12, `uv`, stdlib `argparse`, `osmium-tool`, PyArrow, Shapely, PyProj, PyYAML, pytest, pytest-cov, Ruff, and mypy.

---

## Execution Boundaries

This plan authorizes source code, tests, synthetic fixtures, documentation, and
local Git commits only. It does not authorize:

- reading a real PBF from
  `/Volumes/Seagate M3/projects/osm-polygon-wikidata-only/raw`;
- writing anything under
  `/Volumes/Seagate M3/projects/osm-polygon-description-tag`;
- installing Homebrew packages;
- authenticating to or uploading to Hugging Face;
- pushing Git commits or creating a release.

After Task 12, stop and request independent review. A real-source canary, a full
pipeline run, and publication are three later and separate approval gates.

## File and Responsibility Map

```text
.
├── .gitignore
├── README.md
├── pyproject.toml
├── uv.lock
├── config/
│   └── osmium-export.json
├── docs/
│   ├── dataset-card-template.md
│   └── superpowers/
│       ├── plans/2026-07-27-osm-polygon-description-dataset.md
│       └── specs/2026-07-27-osm-polygon-description-dataset-design.md
├── src/osm_polygon_description_tag/
│   ├── __init__.py
│   ├── py.typed
│   ├── cli.py
│   ├── config.py
│   ├── discovery.py
│   ├── extraction.py
│   ├── transform.py
│   ├── schema.py
│   ├── storage.py
│   ├── manifest.py
│   ├── pipeline.py
│   ├── reporting.py
│   └── publication.py
└── tests/
    ├── fixtures/descriptions.osm
    ├── contracts/
    │   ├── test_cli_contract.py
    │   ├── test_dataset_card.py
    │   └── test_schema_contract.py
    ├── integration/test_end_to_end.py
    ├── test_config.py
    ├── test_discovery.py
    ├── test_extraction.py
    ├── test_manifest.py
    ├── test_pipeline.py
    ├── test_publication.py
    ├── test_reporting.py
    ├── test_storage.py
    └── test_transform.py
```

`config.py` owns defaults and path safety. `discovery.py` owns read-only source
enumeration. `extraction.py` is the only module allowed to invoke `osmium`.
`transform.py` is pure feature-to-record logic. `schema.py` owns the versioned
Arrow/GeoParquet contract. `storage.py` owns temporary Parquet and validation.
`manifest.py` owns artifact identity and resumption. `pipeline.py` composes
these units. `reporting.py` owns data-derived documentation. `publication.py`
owns upload planning and the explicit execution gate. `cli.py` only maps CLI
arguments to these public functions.

### Task 1: Initialize the `uv` Package and Public Baseline

**Files:**
- Create: `.gitignore`
- Create: `README.md`
- Create: `pyproject.toml`
- Create: `src/osm_polygon_description_tag/__init__.py`
- Create: `src/osm_polygon_description_tag/py.typed`
- Create: `tests/test_config.py`

- [ ] **Step 1: Write the failing package-contract test**

```python
from importlib.metadata import metadata
from pathlib import Path

import osm_polygon_description_tag


def test_public_package_metadata_is_complete() -> None:
    project = metadata("osm-polygon-description-tag")
    assert project["Name"] == "osm-polygon-description-tag"
    assert project["License-Expression"] == "Apache-2.0"
    assert osm_polygon_description_tag.__version__ == "0.1.0"
    assert Path("README.md").read_text().startswith("# OSM Polygon Description Tag")
```

- [ ] **Step 2: Run the test and verify RED**

Run: `uv run pytest tests/test_config.py -v`

Expected: FAIL because there is no `pyproject.toml` or importable package.

- [ ] **Step 3: Add the minimal package configuration**

Use this project metadata and no extra runtime dependencies:

```toml
[project]
name = "osm-polygon-description-tag"
version = "0.1.0"
description = "Reproducible GeoParquet of OpenStreetMap polygons with description tags."
readme = "README.md"
requires-python = ">=3.12"
authors = [{ name = "Noé Flandre" }]
license = "Apache-2.0"
dependencies = [
    "pyarrow>=20,<22",
    "pyproj>=3.7,<4",
    "pyyaml>=6,<7",
    "shapely>=2.1,<3",
]

[project.scripts]
osm-polygon-description-tag = "osm_polygon_description_tag.cli:run"

[project.urls]
Source = "https://github.com/NoeFlandre/osm-polygon-description-tag"
Issues = "https://github.com/NoeFlandre/osm-polygon-description-tag/issues"
Dataset = "https://huggingface.co/datasets/NoeFlandre/osm-polygon-description-tag"

[build-system]
requires = ["hatchling"]
build-backend = "hatchling.build"

[tool.hatch.build.targets.wheel]
packages = ["src/osm_polygon_description_tag"]

[dependency-groups]
dev = [
    "mypy>=1.17,<2",
    "pytest>=8.4,<9",
    "pytest-cov>=6.2,<7",
    "ruff>=0.12,<0.13",
]

[tool.pytest.ini_options]
testpaths = ["tests"]
addopts = "-ra --strict-markers"
markers = ["integration: requires the external osmium binary"]

[tool.ruff]
line-length = 100
target-version = "py312"

[tool.ruff.lint]
select = ["E", "F", "I", "B", "UP", "SIM", "RUF", "S", "TID"]
ignore = ["S101"]

[tool.mypy]
python_version = "3.12"
strict = true
packages = ["osm_polygon_description_tag"]

[tool.coverage.run]
branch = true
source = ["osm_polygon_description_tag"]

[tool.coverage.report]
fail_under = 90
show_missing = true
```

Add:

```python
# src/osm_polygon_description_tag/__init__.py
__version__ = "0.1.0"
```

Create a public README stating the scope, the immutable-source rule, the
code/data path separation, Apache-2.0 for code, ODbL for derived data, and all
three approval gates. Use no dataset statistics before artifacts exist.

Ignore only generated local state:

```gitignore
.DS_Store
.coverage
.mypy_cache/
.pytest_cache/
.ruff_cache/
.venv/
__pycache__/
*.py[cod]
build/
dist/
```

- [ ] **Step 4: Lock and verify GREEN**

Run: `uv lock && uv run pytest tests/test_config.py -v`

Expected: PASS and a new `uv.lock`.

- [ ] **Step 5: Run static baseline checks**

Run: `uv run ruff check . && uv run ruff format --check . && uv run mypy`

Expected: all commands exit 0 with no warnings.

- [ ] **Step 6: Commit**

```bash
git add .gitignore README.md pyproject.toml uv.lock src tests/test_config.py
git commit -m "chore: initialize public uv project"
```

### Task 2: Freeze Configuration and Raw-Source Containment

**Files:**
- Create: `src/osm_polygon_description_tag/config.py`
- Modify: `tests/test_config.py`

- [ ] **Step 1: Write failing tests for defaults and containment**

```python
from pathlib import Path

import pytest

from osm_polygon_description_tag.config import Paths, UnsafePathError


def test_paths_use_approved_defaults() -> None:
    paths = Paths.defaults()
    assert paths.source_root == Path(
        "/Volumes/Seagate M3/projects/osm-polygon-wikidata-only/raw"
    )
    assert paths.data_root == Path(
        "/Volumes/Seagate M3/projects/osm-polygon-description-tag"
    )


def test_output_cannot_be_inside_source(tmp_path: Path) -> None:
    source = tmp_path / "raw"
    source.mkdir()
    with pytest.raises(UnsafePathError, match="inside immutable source"):
        Paths(source_root=source, data_root=source / "output").validate()
```

- [ ] **Step 2: Run and verify RED**

Run: `uv run pytest tests/test_config.py -v`

Expected: FAIL importing `osm_polygon_description_tag.config`.

- [ ] **Step 3: Implement the immutable configuration**

```python
from dataclasses import dataclass
from pathlib import Path

DEFAULT_SOURCE_ROOT = Path(
    "/Volumes/Seagate M3/projects/osm-polygon-wikidata-only/raw"
)
DEFAULT_DATA_ROOT = Path(
    "/Volumes/Seagate M3/projects/osm-polygon-description-tag"
)


class UnsafePathError(ValueError):
    pass


def _is_within(child: Path, parent: Path) -> bool:
    return child.resolve(strict=False).is_relative_to(parent.resolve(strict=False))


@dataclass(frozen=True)
class Paths:
    source_root: Path
    data_root: Path

    @classmethod
    def defaults(cls) -> "Paths":
        return cls(DEFAULT_SOURCE_ROOT, DEFAULT_DATA_ROOT)

    def validate(self) -> "Paths":
        if _is_within(self.data_root, self.source_root):
            raise UnsafePathError(
                f"data root is inside immutable source: {self.data_root}"
            )
        if self.source_root.resolve(strict=False) == self.data_root.resolve(strict=False):
            raise UnsafePathError("source root and data root must differ")
        return self
```

- [ ] **Step 4: Verify GREEN and add the inverse containment case**

Run: `uv run pytest tests/test_config.py -v`

Expected: PASS. Then add and pass a test proving that a source directory inside
the data root is also rejected, eliminating ambiguous ownership.

- [ ] **Step 5: Commit**

```bash
git add src/osm_polygon_description_tag/config.py tests/test_config.py
git commit -m "feat: enforce immutable source boundaries"
```

### Task 3: Discover PBF Inputs Deterministically

**Files:**
- Create: `src/osm_polygon_description_tag/discovery.py`
- Create: `tests/test_discovery.py`

- [ ] **Step 1: Write failing discovery tests**

```python
from pathlib import Path

from osm_polygon_description_tag.discovery import discover_sources


def test_discovery_is_direct_sorted_and_pbf_only(tmp_path: Path) -> None:
    (tmp_path / "z-latest.osm.pbf").touch()
    (tmp_path / "a-latest.osm.pbf").touch()
    (tmp_path / "notes.txt").touch()
    nested = tmp_path / "nested"
    nested.mkdir()
    (nested / "ignored.osm.pbf").touch()

    found = discover_sources(tmp_path)

    assert [item.name for item in found] == [
        "a-latest.osm.pbf",
        "z-latest.osm.pbf",
    ]
    assert [item.output_name for item in found] == [
        "a-latest.parquet",
        "z-latest.parquet",
    ]
```

- [ ] **Step 2: Run and verify RED**

Run: `uv run pytest tests/test_discovery.py -v`

Expected: FAIL importing `discovery`.

- [ ] **Step 3: Implement source identity without opening files for writing**

```python
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class Source:
    path: Path
    name: str
    output_name: str
    size_bytes: int
    mtime_ns: int


def discover_sources(source_root: Path) -> tuple[Source, ...]:
    if not source_root.is_dir():
        raise NotADirectoryError(source_root)
    paths = sorted(source_root.glob("*.osm.pbf"), key=lambda path: path.name)
    result: list[Source] = []
    output_names: set[str] = set()
    for path in paths:
        if not path.is_file() or path.is_symlink():
            continue
        output_name = f"{path.name.removesuffix('.osm.pbf')}.parquet"
        if output_name in output_names:
            raise ValueError(f"output collision: {output_name}")
        stat = path.stat()
        result.append(Source(path, path.name, output_name, stat.st_size, stat.st_mtime_ns))
        output_names.add(output_name)
    return tuple(result)
```

- [ ] **Step 4: Verify GREEN**

Run: `uv run pytest tests/test_discovery.py -v`

Expected: PASS, including tests for missing directory, symlink exclusion, empty
inventory, and collision rejection through a focused filename helper.

- [ ] **Step 5: Commit**

```bash
git add src/osm_polygon_description_tag/discovery.py tests/test_discovery.py
git commit -m "feat: discover pbf inputs deterministically"
```

### Task 4: Stream Versioned OSM Areas Through `osmium export`

**Files:**
- Create: `config/osmium-export.json`
- Create: `src/osm_polygon_description_tag/extraction.py`
- Create: `tests/test_extraction.py`

- [ ] **Step 1: Write failing command and parser tests**

```python
from pathlib import Path

from osm_polygon_description_tag.extraction import export_command, parse_copy_record


def test_export_command_has_no_shell_and_uses_pg_copy() -> None:
    command = export_command(
        Path("/input/a.osm.pbf"),
        Path("/repo/config/osmium-export.json"),
        executable="osmium",
    )
    assert command == (
        "osmium", "export", "/input/a.osm.pbf",
        "--output-format", "pg",
        "--config", "/repo/config/osmium-export.json",
        "--output", "-",
    )


def test_copy_parser_keeps_geometry_metadata_and_tags_separate() -> None:
    record = parse_copy_record(
        b"0103000020E6100000...\\tway\\t42\\t3\\t99\\t"
        b"2026-01-01T00:00:00Z\\t{\\\"description\\\":\\\"x\\\","
        b"\\\"__osm_id\\\":\\\"original tag\\\"}\\n"
    )
    assert record.osm_id == 42
    assert record.tags["__osm_id"] == "original tag"
```

- [ ] **Step 2: Run and verify RED**

Run: `uv run pytest tests/test_extraction.py -v`

Expected: FAIL importing `extraction`.

- [ ] **Step 3: Add the exact Osmium configuration**

```json
{
  "attributes": {
    "type": "__osm_type",
    "id": "__osm_id",
    "version": "__osm_version",
    "changeset": "__osm_changeset",
    "timestamp": "__osm_timestamp",
    "uid": false,
    "user": false,
    "way_nodes": false
  },
  "format_options": {
    "tags_type": "json"
  },
  "linear_tags": ["highway", "barrier", "natural=coastline"],
  "area_tags": [
    "aeroway",
    "amenity",
    "building",
    "landuse",
    "leisure",
    "man_made",
    "natural!=coastline"
  ],
  "exclude_tags": [],
  "include_tags": []
}
```

This policy is based on the official `osmium-tool` export example and is
versioned dataset provenance. `area=yes` and `area=no` retain Osmium's explicit
override semantics. OSM has no single exhaustive machine-readable polygon-key
standard, so documentation must name the exact policy rather than claiming an
undefined universal classifier. PostgreSQL COPY keeps attributes in separate
columns from tag JSON, so arbitrary original tag keys are preserved.

- [ ] **Step 4: Implement checked streaming**

```python
import subprocess
from collections.abc import BinaryIO, Iterator
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class ExportRecord:
    geometry_ewkb_hex: str
    osm_type: str
    osm_id: int
    version: int | None
    changeset: int | None
    timestamp: str | None
    tags: dict[str, str]


def export_command(
    source: Path, config: Path, *, executable: str = "osmium"
) -> tuple[str, ...]:
    return (
        executable, "export", str(source),
        "--output-format", "pg",
        "--config", str(config),
        "--output", "-",
    )


def iter_records(stream: BinaryIO) -> Iterator[ExportRecord]:
    for line_number, raw in enumerate(stream, start=1):
        if not raw.strip():
            continue
        try:
            yield parse_copy_record(raw)
        except (UnicodeDecodeError, ValueError) as error:
            raise ValueError(f"invalid COPY record {line_number}: {error}") from error


def osmium_version(executable: str = "osmium") -> str:
    completed = subprocess.run(
        [executable, "--version"],
        check=True,
        capture_output=True,
        text=True,
        timeout=10,
    )
    return completed.stdout.splitlines()[0].strip()
```

Implement `parse_copy_record()` for exactly seven tab-delimited fields:
EWKB hex, type, ID, version, changeset, ISO timestamp, and JSON tags. Decode
PostgreSQL COPY escapes (`\\\\`, `\\t`, `\\n`, `\\r`) before JSON parsing and
interpret `\\N` as null only for nullable metadata columns.

Add `stream_export()` using `subprocess.Popen(..., shell=False, stdout=PIPE,
stderr=PIPE)`. It must drain stderr on a dedicated thread, cap retained stderr
at 1 MiB, terminate the child when the consumer raises, wait with a finite
timeout, and raise a typed `OsmiumExportError` on non-zero exit.

- [ ] **Step 5: Verify GREEN**

Run: `uv run pytest tests/test_extraction.py -v`

Expected: PASS for parsing, malformed JSON, wrong field counts, missing binary,
timeout, non-zero exit, bounded diagnostics, and consumer cancellation.

- [ ] **Step 6: Commit**

```bash
git add config/osmium-export.json src/osm_polygon_description_tag/extraction.py tests/test_extraction.py
git commit -m "feat: stream versioned osm area exports"
```

### Task 5: Transform Export Records Into Typed Polygon Records

**Files:**
- Create: `src/osm_polygon_description_tag/transform.py`
- Create: `tests/test_transform.py`

- [ ] **Step 1: Write the first failing description test**

```python
from osm_polygon_description_tag.transform import descriptions_from_tags


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
```

- [ ] **Step 2: Run and verify RED**

Run: `uv run pytest tests/test_transform.py::test_descriptions_preserve_base_suffixes_and_values -v`

Expected: FAIL importing `transform`.

- [ ] **Step 3: Implement exact matching**

```python
def descriptions_from_tags(
    tags: dict[str, str],
) -> tuple[str | None, dict[str, str]]:
    base = tags.get("description")
    if base is not None and not base.strip():
        base = None
    localized = {
        key.removeprefix("description:"): value
        for key, value in sorted(tags.items())
        if key.startswith("description:")
        and key != "description:"
        and value.strip()
    }
    return base, localized
```

- [ ] **Step 4: Verify GREEN**

Run the focused test again and expect PASS.

- [ ] **Step 5: Write failing geometry and metadata tests**

Use an `ExportRecord` with a Polygon containing one hole and another with a
two-part MultiPolygon. Assert:

```python
record = transform_record(export_record, "fixture.osm.pbf")
assert record["osm_type"] == "relation"
assert record["osm_id"] == 42
assert record["osm_url"] == "https://www.openstreetmap.org/relation/42"
assert record["geometry_type"] == "MultiPolygon"
assert record["area_m2"] > 0
assert record["bbox_min_x"] == 0.0
assert record["tags"]["name"] == "Preserved"
assert "__osm_id" not in record["tags"]
```

Also assert stable rejection codes for no non-empty description, non-polygon
geometry, empty geometry, invalid geometry, and missing attributes.

- [ ] **Step 6: Implement minimal geometry transformation**

```python
from dataclasses import dataclass
from pyproj import Geod
from shapely import from_wkb, to_wkb
from shapely.geometry.base import BaseGeometry

from osm_polygon_description_tag.extraction import ExportRecord

GEOD = Geod(ellps="WGS84")


@dataclass(frozen=True)
class RejectedFeature(Exception):
    reason: str


def geodesic_area_m2(geometry: BaseGeometry) -> float:
    area, _ = GEOD.geometry_area_perimeter(geometry)
    return abs(float(area))


def transform_record(record: ExportRecord, source_pbf: str) -> dict[str, object]:
    tags = record.tags
    base, localized = descriptions_from_tags(tags)
    if base is None and not localized:
        raise RejectedFeature("no_nonempty_description")
    geometry = from_wkb(bytes.fromhex(record.geometry_ewkb_hex))
    if geometry.geom_type not in {"Polygon", "MultiPolygon"}:
        raise RejectedFeature("non_polygon_geometry")
    if geometry.is_empty or not geometry.is_valid:
        raise RejectedFeature("invalid_geometry")
    area = geodesic_area_m2(geometry)
    if not area > 0:
        raise RejectedFeature("nonpositive_area")
    min_x, min_y, max_x, max_y = geometry.bounds
    osm_type = record.osm_type
    osm_id = record.osm_id
    return {
        "source_pbf": source_pbf,
        "osm_type": osm_type,
        "osm_id": osm_id,
        "osm_url": f"https://www.openstreetmap.org/{osm_type}/{osm_id}",
        "version": record.version,
        "changeset": record.changeset,
        "timestamp": _optional_timestamp(record.timestamp),
        "description": base,
        "localized_descriptions": localized,
        "tags": tags,
        "geometry_type": geometry.geom_type,
        "area_m2": area,
        "bbox_min_x": min_x,
        "bbox_min_y": min_y,
        "bbox_max_x": max_x,
        "bbox_max_y": max_y,
        "geometry": to_wkb(geometry, hex=False, output_dimension=2),
    }
```

Implement `_optional_int` and `_optional_timestamp` as strict converters.
Normalize timestamps to timezone-aware UTC `datetime` values. Validate
`osm_type in {"way", "relation"}` and positive IDs.

- [ ] **Step 7: Verify GREEN**

Run: `uv run pytest tests/test_transform.py -v`

Expected: all transformation tests PASS.

- [ ] **Step 8: Commit**

```bash
git add src/osm_polygon_description_tag/transform.py tests/test_transform.py
git commit -m "feat: transform described polygon features"
```

### Task 6: Freeze Arrow and GeoParquet Contracts

**Files:**
- Create: `src/osm_polygon_description_tag/schema.py`
- Create: `tests/contracts/test_schema_contract.py`

- [ ] **Step 1: Write the failing schema snapshot test**

```python
import pyarrow as pa

from osm_polygon_description_tag.schema import SCHEMA, geo_metadata


def test_arrow_schema_is_frozen() -> None:
    assert SCHEMA.names == [
        "source_pbf", "osm_type", "osm_id", "osm_url", "version",
        "changeset", "timestamp", "description", "localized_descriptions",
        "tags", "geometry_type", "area_m2", "bbox_min_x", "bbox_min_y",
        "bbox_max_x", "bbox_max_y", "geometry",
    ]
    assert SCHEMA.field("geometry").nullable is False
    assert SCHEMA.field("tags").type.key_type == pa.string()


def test_geo_metadata_is_geoparquet_1_1() -> None:
    metadata = geo_metadata(["Polygon", "MultiPolygon"], [-1.0, -2.0, 3.0, 4.0])
    assert metadata["version"] == "1.1.0"
    assert metadata["primary_column"] == "geometry"
    assert metadata["columns"]["geometry"]["encoding"] == "WKB"
    assert "crs" not in metadata["columns"]["geometry"]
```

- [ ] **Step 2: Run and verify RED**

Run: `uv run pytest tests/contracts/test_schema_contract.py -v`

Expected: FAIL importing `schema`.

- [ ] **Step 3: Implement the exact Arrow schema**

```python
import pyarrow as pa

SCHEMA_VERSION = 1
SCHEMA = pa.schema([
    pa.field("source_pbf", pa.string(), nullable=False),
    pa.field("osm_type", pa.string(), nullable=False),
    pa.field("osm_id", pa.int64(), nullable=False),
    pa.field("osm_url", pa.string(), nullable=False),
    pa.field("version", pa.int32()),
    pa.field("changeset", pa.int64()),
    pa.field("timestamp", pa.timestamp("ms", tz="UTC")),
    pa.field("description", pa.string()),
    pa.field("localized_descriptions", pa.map_(pa.string(), pa.string()), nullable=False),
    pa.field("tags", pa.map_(pa.string(), pa.string()), nullable=False),
    pa.field("geometry_type", pa.string(), nullable=False),
    pa.field("area_m2", pa.float64(), nullable=False),
    pa.field("bbox_min_x", pa.float64(), nullable=False),
    pa.field("bbox_min_y", pa.float64(), nullable=False),
    pa.field("bbox_max_x", pa.float64(), nullable=False),
    pa.field("bbox_max_y", pa.float64(), nullable=False),
    pa.field("geometry", pa.binary(), nullable=False),
])


def geo_metadata(
    geometry_types: list[str], bbox: list[float]
) -> dict[str, object]:
    return {
        "version": "1.1.0",
        "primary_column": "geometry",
        "columns": {
            "geometry": {
                "encoding": "WKB",
                "geometry_types": sorted(set(geometry_types)),
                "bbox": bbox,
                "covering": {
                    "bbox": {
                        "xmin": ["bbox_min_x"],
                        "ymin": ["bbox_min_y"],
                        "xmax": ["bbox_max_x"],
                        "ymax": ["bbox_max_y"],
                    }
                },
            }
        },
    }
```

Missing `crs` deliberately means OGC:CRS84 longitude/latitude. Do not claim
ring orientation or spherical edges without normalizing geometry accordingly.

- [ ] **Step 4: Verify GREEN**

Run: `uv run pytest tests/contracts/test_schema_contract.py -v`

Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add src/osm_polygon_description_tag/schema.py tests/contracts/test_schema_contract.py
git commit -m "feat: freeze geoparquet schema contract"
```

### Task 7: Write and Validate GeoParquet Atomically

**Files:**
- Create: `src/osm_polygon_description_tag/storage.py`
- Create: `tests/test_storage.py`

- [ ] **Step 1: Write the failing atomic-write test**

```python
from pathlib import Path

import pytest

from osm_polygon_description_tag.storage import write_geoparquet


def test_failed_validation_never_promotes_output(
    tmp_path: Path, valid_records: list[dict[str, object]]
) -> None:
    target = tmp_path / "region.parquet"
    with pytest.raises(ValueError, match="forced validation failure"):
        write_geoparquet(
            iter(valid_records),
            target,
            batch_size=2,
            validator=lambda _: (_ for _ in ()).throw(
                ValueError("forced validation failure")
            ),
        )
    assert not target.exists()
    assert list(tmp_path.glob("*.tmp")) == []
```

- [ ] **Step 2: Run and verify RED**

Run: `uv run pytest tests/test_storage.py -v`

Expected: FAIL importing `storage`.

- [ ] **Step 3: Implement bounded Parquet writing**

Implement `write_geoparquet(records, target, batch_size, validator)` with:

```python
temporary = target.with_name(f".{target.name}.{uuid.uuid4().hex}.tmp")
writer = pq.ParquetWriter(
    temporary,
    schema_with_geo_metadata,
    compression="zstd",
    use_dictionary=["source_pbf", "osm_type", "geometry_type"],
)
```

Buffer at most `batch_size` records, construct each `RecordBatch` with
`pa.RecordBatch.from_pylist(batch, schema=SCHEMA)`, and maintain aggregate bbox
and geometry types. Because file metadata is known only after streaming, first
write the temporary Parquet, then use `pq.read_table` and rewrite once to a
second owned temporary with final GeoParquet metadata. Validate the second
temporary, `fsync` it and its directory, then `os.replace` it onto the target.
Always remove only the two exact owned temporary paths in `finally`.

- [ ] **Step 4: Implement full artifact validation**

`validate_geoparquet(path)` must inspect Parquet metadata and batches without
loading the full file. Assert:

- exact Arrow field names/types/nullability;
- parseable `geo` JSON conforming to the project’s required 1.1 fields;
- actual geometry types equal metadata geometry types;
- actual aggregate bbox equals metadata bbox within floating precision;
- unique `(osm_type, osm_id)` within the file;
- non-null geometry, positive finite area, and ordered finite bboxes;
- WKB decodes to valid Polygon/MultiPolygon;
- row count returned to the caller.

- [ ] **Step 5: Verify GREEN**

Run: `uv run pytest tests/test_storage.py -v`

Expected: PASS for empty files, one/multiple batches, corrupt WKB, wrong schema,
duplicate identity, invalid metadata, forced interruption, and atomic promotion.

- [ ] **Step 6: Commit**

```bash
git add src/osm_polygon_description_tag/storage.py tests/test_storage.py
git commit -m "feat: write validated geoparquet atomically"
```

### Task 8: Create Factual Manifests and Resumption Decisions

**Files:**
- Create: `src/osm_polygon_description_tag/manifest.py`
- Create: `tests/test_manifest.py`

- [ ] **Step 1: Write failing identity tests**

```python
from pathlib import Path

from osm_polygon_description_tag.manifest import (
    file_sha256,
    is_resumable,
)


def test_sha256_reads_without_mutating(tmp_path: Path) -> None:
    path = tmp_path / "artifact"
    path.write_bytes(b"abc")
    before = path.stat()
    assert file_sha256(path) == (
        "ba7816bf8f01cfea414140de5dae2223"
        "b00361a396177a9cb410ff61f20015ad"
    )
    assert path.stat().st_mtime_ns == before.st_mtime_ns


def test_resumption_requires_matching_source_and_output_checksums(
    valid_manifest: dict[str, object],
) -> None:
    assert is_resumable(valid_manifest, valid_manifest["source"], valid_manifest["output"])
    changed = {**valid_manifest["source"], "size_bytes": 1}
    assert not is_resumable(valid_manifest, changed, valid_manifest["output"])
```

- [ ] **Step 2: Run and verify RED**

Run: `uv run pytest tests/test_manifest.py -v`

Expected: FAIL importing `manifest`.

- [ ] **Step 3: Implement versioned JSON manifests**

Use frozen dataclasses `SourceIdentity`, `OutputIdentity`, `RunCounts`, and
`Manifest`. Serialize canonical UTF-8 JSON using:

```python
json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n"
```

Use SHA-256 with 8 MiB read chunks. Write the manifest to an owned temporary,
`fsync`, and atomically replace only after the Parquet is finalized. Record:
schema versions, file identities, `osmium --version`, Python dependency
versions, code Git revision if available, UTC start/completion instants,
emitted feature count, included row count, and rejection counts.

- [ ] **Step 4: Verify GREEN**

Run: `uv run pytest tests/test_manifest.py -v`

Expected: PASS for deterministic JSON, checksum mismatches, unsupported
manifest version, missing output, corrupt JSON, and source identity drift.

- [ ] **Step 5: Commit**

```bash
git add src/osm_polygon_description_tag/manifest.py tests/test_manifest.py
git commit -m "feat: record artifact identity and resumability"
```

### Task 9: Compose One-File and Resumable Pipelines

**Files:**
- Create: `src/osm_polygon_description_tag/pipeline.py`
- Create: `tests/test_pipeline.py`

- [ ] **Step 1: Write the failing one-file orchestration test**

Use injected `exporter`, `writer`, and `clock` callables:

```python
result = build_one(
    source,
    paths,
    export_config=Path("config/osmium-export.json"),
    exporter=lambda *_: iter([described_polygon, irrelevant_polygon]),
    writer=fake_writer,
    clock=frozen_clock,
)
assert result.included_rows == 1
assert result.rejections == {"no_nonempty_description": 1}
assert fake_writer.target == paths.data_root / "data" / source.output_name
```

Assert that output and manifest paths are never under `source_root`.

- [ ] **Step 2: Run and verify RED**

Run: `uv run pytest tests/test_pipeline.py -v`

Expected: FAIL importing `pipeline`.

- [ ] **Step 3: Implement `build_one`**

`build_one` performs, in order:

1. validate `Paths`;
2. verify that the selected `Source` is a discovered direct child;
3. create only `<data-root>/data` and `<data-root>/manifests`;
4. return `SKIPPED` only after manifest and Parquet revalidation;
5. stream features, count emitted records, transform, and count typed
   `RejectedFeature` reasons;
6. atomically write and validate Parquet;
7. compute identities and atomically write the manifest;
8. return an immutable `BuildResult`.

If manifest finalization fails after Parquet promotion, keep the valid Parquet
but report failure; the next invocation must treat it as incomplete and
recreate/validate it before writing a manifest.

- [ ] **Step 4: Write the failing `build_all` ordering test**

```python
results = build_all(
    (source_z, source_a),
    build=lambda source: seen.append(source.name) or success(source),
)
assert seen == ["a-latest.osm.pbf", "z-latest.osm.pbf"]
assert [result.source_name for result in results] == seen
```

- [ ] **Step 5: Implement conservative orchestration**

Implement sequential `build_all` only. It stops after the first infrastructure
failure and returns completed results plus the failing source. Do not add
parallelism until measurements demonstrate that concurrent Osmium area
assembly fits memory and improves throughput.

- [ ] **Step 6: Verify GREEN**

Run: `uv run pytest tests/test_pipeline.py -v`

Expected: PASS for success, skip, stale rebuild, typed rejection, exporter
failure, writer failure, manifest failure, deterministic ordering, and
fail-fast behavior.

- [ ] **Step 7: Commit**

```bash
git add src/osm_polygon_description_tag/pipeline.py tests/test_pipeline.py
git commit -m "feat: compose resumable polygon builds"
```

### Task 10: Generate Artifact-Derived Statistics and Dataset Card

**Files:**
- Create: `docs/dataset-card-template.md`
- Create: `src/osm_polygon_description_tag/reporting.py`
- Create: `tests/test_reporting.py`
- Create: `tests/contracts/test_dataset_card.py`

- [ ] **Step 1: Write failing aggregate-statistics tests**

Create two tiny validated Parquets in `tmp_path` and assert exact output:

```python
stats = collect_stats(data_root)
assert stats["output_files"] == 2
assert stats["rows"] == 3
assert stats["osm_types"] == {"relation": 1, "way": 2}
assert stats["geometry_types"] == {"MultiPolygon": 1, "Polygon": 2}
assert stats["description_suffixes"] == {"en": 2, "pt-BR": 1}
assert stats["rejections"] == {"no_nonempty_description": 4}
```

- [ ] **Step 2: Run and verify RED**

Run: `uv run pytest tests/test_reporting.py -v`

Expected: FAIL importing `reporting`.

- [ ] **Step 3: Implement streaming statistics**

Read only columns required for each metric using
`pyarrow.parquet.ParquetFile.iter_batches`. Merge only manifests whose source
and output identities validate. Compute deterministic counts, exact suffix
frequencies, byte totals, min/max/median and fixed p25/p75 area quantiles, data
timestamp range, and UTC generation timestamp. Store units in field names.
Reject missing, stale, or extra artifact/manifest pairs.

- [ ] **Step 4: Write the card template and failing renderer test**

The template begins:

```markdown
---
pretty_name: OSM Polygon Description Tag
license: odbl
language:
- multilingual
tags:
- geospatial
- openstreetmap
- geoparquet
---

# OSM Polygon Description Tag

<!-- GENERATED:STATS:START -->
<!-- GENERATED:STATS:END -->
```

Handwritten sections cover summary, schema, loading with PyArrow/GeoPandas,
source and methodology, attribution, license, intended uses, limitations,
regional overlap, description-suffix caveats, reproducibility, and contact.
Assert the renderer changes only the marked generated block and that every
number inside it is rendered from the supplied stats object.

- [ ] **Step 5: Implement deterministic generation**

`generate_dataset_docs(data_root, template_path)` writes canonical
`stats.json` and `README.md` atomically under the data root. Render sorted
tables with comma-grouped integer counts and explicit area units. Embed the
stats JSON SHA-256 and schema version in the generated block.

- [ ] **Step 6: Verify GREEN**

Run:

`uv run pytest tests/test_reporting.py tests/contracts/test_dataset_card.py -v`

Expected: PASS for valid aggregation, stale artifacts, empty dataset, exact
suffix preservation, deterministic output with a frozen clock, generated
markers, YAML metadata, attribution, limitations, and no unbacked numbers.

- [ ] **Step 7: Commit**

```bash
git add docs/dataset-card-template.md src/osm_polygon_description_tag/reporting.py tests/test_reporting.py tests/contracts/test_dataset_card.py
git commit -m "feat: generate factual dataset documentation"
```

### Task 11: Create a Non-Destructive Hugging Face Publication Gate

**Files:**
- Create: `src/osm_polygon_description_tag/publication.py`
- Create: `tests/test_publication.py`

- [ ] **Step 1: Write failing upload-plan tests**

```python
plan = create_upload_plan(data_root)
assert [item.relative_path for item in plan.files] == [
    "README.md",
    "data/a-latest.parquet",
    "manifests/a-latest.manifest.json",
    "stats.json",
]
assert plan.repo_id == "NoeFlandre/osm-polygon-description-tag"
assert len(plan.identity_sha256) == 64
```

Add failures for symlinks, unknown top-level paths, temporary files, checksum
drift, stale manifests, and missing card/stats.

- [ ] **Step 2: Run and verify RED**

Run: `uv run pytest tests/test_publication.py -v`

Expected: FAIL importing `publication`.

- [ ] **Step 3: Implement the allowlisted plan**

Allow only:

- `README.md`;
- `stats.json`;
- `data/*.parquet`;
- `manifests/*.manifest.json`.

Canonicalize relative paths, forbid symlinks, include file size and SHA-256,
and hash canonical plan JSON to get `identity_sha256`. Serialize plans under
`<data-root>/publication-plans/` only when explicitly requested.

- [ ] **Step 4: Write failing execution-gate tests**

Assert that `execute_upload(plan, confirmation, runner)` refuses any
confirmation other than the exact plan SHA-256, rechecks every file identity,
and then passes this tuple to `runner`:

```python
(
    "hf", "upload-large-folder",
    "NoeFlandre/osm-polygon-description-tag",
    str(data_root),
    "--repo-type", "dataset",
    "--include", "README.md",
    "--include", "stats.json",
    "--include", "data/*.parquet",
    "--include", "manifests/*.manifest.json",
)
```

- [ ] **Step 5: Implement execution without implicit authentication**

Use `subprocess.run(list(command), check=True, shell=False)`. Never call
`hf auth login`, never accept a token argument, never use `--delete`, and never
execute unless the exact unchanged plan identity is confirmed.

- [ ] **Step 6: Verify GREEN**

Run: `uv run pytest tests/test_publication.py -v`

Expected: PASS without network access; the runner is injected and records the
command rather than invoking `hf`.

- [ ] **Step 7: Commit**

```bash
git add src/osm_polygon_description_tag/publication.py tests/test_publication.py
git commit -m "feat: gate dataset publication by artifact plan"
```

### Task 12: Freeze the Public CLI and Prove Synthetic End-to-End Behavior

**Files:**
- Create: `src/osm_polygon_description_tag/cli.py`
- Create: `tests/contracts/test_cli_contract.py`
- Create: `tests/fixtures/descriptions.osm`
- Create: `tests/integration/test_end_to_end.py`
- Modify: `README.md`

- [ ] **Step 1: Write the failing CLI contract test**

Invoke `run([...])` and assert exact subcommands:

```python
assert set(parser._subparsers._group_actions[0].choices) == {
    "inspect", "build-one", "build-all", "validate",
    "generate-card", "publish-plan", "publish",
}
```

Assert `inspect` defaults to approved paths, `build-one` requires a basename,
`publish` requires `--plan` and `--confirm`, and every failure exits non-zero
with a concise actionable stderr message.

- [ ] **Step 2: Run and verify RED**

Run: `uv run pytest tests/contracts/test_cli_contract.py -v`

Expected: FAIL importing `cli`.

- [ ] **Step 3: Implement a thin `argparse` CLI**

Build the parser in `create_parser()`. Implement:

```python
def run(argv: Sequence[str] | None = None) -> int:
    parser = create_parser()
    args = parser.parse_args(argv)
    try:
        return int(args.handler(args))
    except (OSError, ValueError, PipelineError) as error:
        parser.exit(1, f"error: {error}\n")
```

Handlers construct `Paths`, call one public module function, and print
machine-readable JSON summaries with sorted keys. `inspect` is read-only.
`validate` is read-only. The build commands create data-root artifacts.

- [ ] **Step 4: Add the synthetic OSM fixture**

Use OSM XML with explicit nodes and IDs for:

- one `building=yes` closed way with `description`;
- one closed `barrier=fence` way with `description` and no area tag;
- one `area=no` closed way;
- one multipolygon relation with an inner ring and `description:en`;
- one `description:pt-BR`;
- whitespace-only descriptions;
- an open described way;
- an unrelated node.

All fixture content is synthetic and documented as such.

- [ ] **Step 5: Write the integration test before running it**

The test skips only when `shutil.which("osmium") is None`. It creates a
temporary source and data root, copies the fixture to the source root, invokes
the real CLI, and asserts:

- the expected included IDs and excluded linear/open IDs;
- Polygon/MultiPolygon geometry and positive area;
- hole-sensitive area smaller than the outer-only comparison;
- all original tags and exact suffixes;
- GeoParquet metadata validation;
- matching manifest checksums;
- exact generated stats;
- ODbL and OpenStreetMap contributor attribution in the generated card;
- no file in the source root changed by content, size, or mtime.

- [ ] **Step 6: Run and observe the expected environment gate**

Run: `uv run pytest tests/integration/test_end_to_end.py -v`

Expected on the current machine before a separately authorized Osmium install:
SKIP with `osmium executable is required`. A skip is not acceptance evidence
for Task 12.

- [ ] **Step 7: Complete public documentation**

Document:

```bash
uv sync
brew install osmium-tool
uv run osm-polygon-description-tag inspect
uv run osm-polygon-description-tag build-one afghanistan-latest.osm.pbf
uv run osm-polygon-description-tag validate
uv run osm-polygon-description-tag generate-card
uv run osm-polygon-description-tag publish-plan
```

Mark build, real-source examples, and publication as commands users may choose,
not commands executed during implementation. Explain that `publish` needs the
exact plan identity and existing `hf` authentication.

- [ ] **Step 8: Run the complete local acceptance suite**

Run:

```bash
uv run ruff check .
uv run ruff format --check .
uv run mypy
uv run pytest --cov=osm_polygon_description_tag --cov-report=term-missing
```

Expected: lint, format, and types PASS; all unit/contract tests PASS; coverage
is at least 90%; integration is either PASS with an already installed Osmium
or is explicitly reported as unaccepted due to SKIP.

- [ ] **Step 9: Commit**

```bash
git add src/osm_polygon_description_tag/cli.py tests README.md
git commit -m "feat: complete synthetic end-to-end pipeline"
```

## Independent Review Gate

After Task 12:

1. Inspect actual Git diff and commit history.
2. Run the complete local acceptance suite fresh.
3. If `osmium` exists, run the synthetic integration test fresh.
4. Verify the raw source tree identity was not changed.
5. Review upload command construction without executing it.
6. Review dataset-card claims against generated fixture artifacts.
7. Stop and report findings.

Do not install `osmium-tool` merely to clear the integration gate. Do not run a
real-source canary. Do not push Git commits. Do not upload to Hugging Face.
