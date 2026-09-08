"""Additive ``language-v1`` export and guarded publication.

Language annotations are published as a *separate, additive* configuration.
The original default dataset, its GeoParquet files, and schema 3 are never
touched: the upload allowlist built here can only ever contain paths under the
``language-v1`` prefix, and nothing in this module deletes or reconciles remote
files.

Every number that reaches the dataset card is derived from the exported
annotation files themselves, never from an estimate. Scores are raw detector
outputs, so the generated prose deliberately makes no accuracy or calibration
claim.
"""

import json
import re
from collections import Counter
from collections.abc import Iterator, Sequence
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Final

import pyarrow as pa
import pyarrow.parquet as pq

from osm_polygon_description_tag.dataset.languages.annotations import (
    ANNOTATION_COMPRESSION,
    ANNOTATION_SCHEMA,
    ANNOTATION_SCHEMA_VERSION,
    read_annotation_part,
)
from osm_polygon_description_tag.dataset.languages.atomic import (
    atomic_write_json,
    atomic_write_via,
    canonical_json_bytes,
)
from osm_polygon_description_tag.dataset.languages.checkpoint import (
    ShardPaths,
    exclusive_worker_lock,
    read_checkpoint,
    shard_paths,
)
from osm_polygon_description_tag.dataset.languages.payloads import PayloadReader, require_object
from osm_polygon_description_tag.dataset.languages.snapshot import SnapshotManifest, read_snapshot
from osm_polygon_description_tag.dataset.languages.validation import validate_run
from osm_polygon_description_tag.dataset.manifest import file_sha256
from osm_polygon_description_tag.publication.models import PublicationError, UploadItem, UploadPlan
from osm_polygon_description_tag.publication.planning import file_sha256_bytes

LANGUAGE_CONFIG_NAME: Final = "language-v1"
LANGUAGE_REMOTE_PREFIX: Final = "language-v1"
LANGUAGE_DATA_PREFIX: Final = f"{LANGUAGE_REMOTE_PREFIX}/data"
LANGUAGE_STATS_PATH: Final = f"{LANGUAGE_REMOTE_PREFIX}/stats.json"
LANGUAGE_MANIFEST_PATH: Final = f"{LANGUAGE_REMOTE_PREFIX}/export-manifest.json"
EXPORT_BATCH_ROWS: Final = 4096
_SAFE_NAME = re.compile(r"[^a-z0-9]+")


class LanguagePublicationError(PublicationError):
    """Raised when a language export or its upload plan is not publishable."""


@dataclass(frozen=True, slots=True)
class LanguageStats:
    """Counts derived from the exported annotation files themselves."""

    annotation_count: int
    object_count: int
    base_description_count: int
    localized_description_count: int
    detected_count: int
    uncertain_count: int
    non_linguistic_count: int
    distinct_language_count: int
    top_languages: tuple[tuple[str, int], ...]

    def to_payload(self) -> dict[str, object]:
        return {
            "annotation_schema_version": ANNOTATION_SCHEMA_VERSION,
            "annotation_count": self.annotation_count,
            "object_count": self.object_count,
            "base_description_count": self.base_description_count,
            "localized_description_count": self.localized_description_count,
            "detected_count": self.detected_count,
            "uncertain_count": self.uncertain_count,
            "non_linguistic_count": self.non_linguistic_count,
            "distinct_language_count": self.distinct_language_count,
            "top_languages": [
                {"language_code": code, "annotation_count": count}
                for code, count in self.top_languages
            ],
        }


@dataclass(frozen=True, slots=True)
class LanguageExport:
    """A validated, additive export ready to be planned for upload."""

    export_root: Path
    config_name: str
    snapshot_id: str
    model_config_fingerprint: str
    library_name: str
    library_version: str
    files: tuple[str, ...]
    stats: LanguageStats

    def to_payload(self) -> dict[str, object]:
        return {
            "config_name": self.config_name,
            "snapshot_id": self.snapshot_id,
            "model_config_fingerprint": self.model_config_fingerprint,
            "library_name": self.library_name,
            "library_version": self.library_version,
            "files": list(self.files),
            "stats": self.stats.to_payload(),
        }


class _StatsAccumulator:
    """Accumulate exported-row statistics without holding the rows."""

    __slots__ = ("_languages", "_objects", "_statuses", "_tag_keys", "_total")

    def __init__(self) -> None:
        self._total = 0
        self._objects: set[tuple[str, int]] = set()
        self._statuses: Counter[str] = Counter()
        self._tag_keys: Counter[str] = Counter()
        self._languages: Counter[str] = Counter()

    def observe(self, batch: pa.RecordBatch) -> None:
        """Record one exported batch."""
        self._total += batch.num_rows
        columns = batch.to_pydict()
        self._statuses.update(columns["status"])
        self._languages.update(code for code in columns["language_code"] if code is not None)
        self._tag_keys.update(
            "base" if key == "description" else "localized" for key in columns["tag_key"]
        )
        self._objects.update(zip(columns["osm_type"], columns["osm_id"], strict=True))

    def result(self) -> LanguageStats:
        """Return the accumulated statistics."""
        return LanguageStats(
            annotation_count=self._total,
            object_count=len(self._objects),
            base_description_count=self._tag_keys["base"],
            localized_description_count=self._tag_keys["localized"],
            detected_count=self._statuses["detected"],
            uncertain_count=self._statuses["uncertain"],
            non_linguistic_count=self._statuses["non_linguistic"],
            distinct_language_count=len(self._languages),
            top_languages=tuple(
                sorted(self._languages.items(), key=lambda item: (-item[1], item[0]))[:20]
            ),
        )


def _export_name(shard: str) -> str:
    stem = _SAFE_NAME.sub("-", Path(shard).stem.lower()).strip("-")
    if not stem:
        raise LanguagePublicationError(f"shard name cannot be expressed as a file name: {shard}")
    return f"{stem}.parquet"


def _committed_parts(paths: ShardPaths) -> tuple[str, ...]:
    return read_checkpoint(paths.checkpoint).completed_parts


def _stream_parts(paths: ShardPaths, part_names: Sequence[str]) -> Iterator[pa.RecordBatch]:
    for part_name in part_names:
        table = read_annotation_part(paths.part(part_name))
        yield from table.to_batches(max_chunksize=EXPORT_BATCH_ROWS)


def _write_shard_export(
    target: Path, paths: ShardPaths, part_names: Sequence[str], stats: _StatsAccumulator
) -> None:
    def _produce(temp: Path) -> None:
        with pq.ParquetWriter(
            temp, ANNOTATION_SCHEMA, compression=ANNOTATION_COMPRESSION
        ) as writer:
            for batch in _stream_parts(paths, part_names):
                stats.observe(batch)
                writer.write_batch(batch)

    atomic_write_via(target, _produce)


def _require_complete_run(run_dir: Path) -> SnapshotManifest:
    report = validate_run(run_dir)
    if not report.is_complete:
        problems = list(report.issues) + [
            issue for shard in report.shards for issue in shard.issues
        ]
        detail = problems[0] if problems else "not every shard is complete"
        raise LanguagePublicationError(f"run is not publishable: {detail}")
    return read_snapshot(run_dir)


def export_language_annotations(run_dir: Path, export_root: Path) -> LanguageExport:
    """Export one complete run into an additive ``language-v1`` tree.

    Refuses to export anything unless every snapshot shard validates as
    complete, so a partially processed run can never be published as if it
    covered the whole dataset.
    """
    with exclusive_worker_lock(export_root / LANGUAGE_REMOTE_PREFIX):
        return _export_language_annotations(run_dir, export_root)


def _export_language_annotations(run_dir: Path, export_root: Path) -> LanguageExport:
    snapshot = _require_complete_run(run_dir)
    data_dir = export_root / LANGUAGE_DATA_PREFIX
    _require_contained_export_path(export_root, data_dir)
    atomic_write_json(
        export_root / LANGUAGE_MANIFEST_PATH,
        {"schema_version": 1, "status": "in_progress", "snapshot_id": snapshot.snapshot_id},
    )
    stats = _StatsAccumulator()
    names: list[str] = []
    for source in snapshot.source_files:
        paths = shard_paths(run_dir, source.relative_path)
        name = _export_name(source.relative_path)
        if name in names:
            raise LanguagePublicationError(f"two shards export to the same file name: {name}")
        _write_shard_export(data_dir / name, paths, _committed_parts(paths), stats)
        names.append(name)
    export = LanguageExport(
        export_root=export_root,
        config_name=LANGUAGE_CONFIG_NAME,
        snapshot_id=snapshot.snapshot_id,
        model_config_fingerprint=snapshot.model_config_fingerprint,
        library_name=snapshot.model_identity.library_name,
        library_version=snapshot.model_identity.library_version,
        files=tuple(f"{LANGUAGE_DATA_PREFIX}/{name}" for name in sorted(names)),
        stats=stats.result(),
    )
    atomic_write_json(export_root / LANGUAGE_STATS_PATH, export.to_payload())
    atomic_write_json(export_root / LANGUAGE_MANIFEST_PATH, _export_seal(export))
    return export


def _export_seal(export: LanguageExport) -> dict[str, object]:
    paths = (*export.files, LANGUAGE_STATS_PATH)
    _validate_allowlist(paths)
    return {
        "schema_version": 1,
        "files": [asdict(_upload_item(export.export_root, path)) for path in sorted(paths)],
    }


def _validate_export_seal(export: LanguageExport) -> None:
    path = export.export_root / LANGUAGE_MANIFEST_PATH
    try:
        recorded = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as error:
        raise LanguagePublicationError(f"cannot read completed export manifest: {error}") from error
    if canonical_json_bytes(recorded) != canonical_json_bytes(_export_seal(export)):
        raise LanguagePublicationError("export files changed after the completed export manifest")


def _stats_from_payload(reader: PayloadReader) -> LanguageStats:
    values = reader.reader("stats")
    if values.integer("annotation_schema_version") != ANNOTATION_SCHEMA_VERSION:
        raise LanguagePublicationError("unsupported annotation schema version in export stats")
    return LanguageStats(
        annotation_count=values.integer("annotation_count"),
        object_count=values.integer("object_count"),
        base_description_count=values.integer("base_description_count"),
        localized_description_count=values.integer("localized_description_count"),
        detected_count=values.integer("detected_count"),
        uncertain_count=values.integer("uncertain_count"),
        non_linguistic_count=values.integer("non_linguistic_count"),
        distinct_language_count=values.integer("distinct_language_count"),
        top_languages=_top_languages(values),
    )


def _top_languages(values: PayloadReader) -> tuple[tuple[str, int], ...]:
    entries: list[tuple[str, int]] = []
    for item in values.items("top_languages"):
        entry = require_object(item, error=LanguagePublicationError, label="top language")
        entries.append((entry.text("language_code"), entry.integer("annotation_count")))
    return tuple(entries)


def read_language_export(export_root: Path) -> LanguageExport:
    """Rebuild a previously written export description from its stats file."""
    path = export_root / LANGUAGE_STATS_PATH
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as error:
        raise LanguagePublicationError(f"cannot read language export {path}: {error}") from error
    reader = require_object(payload, error=LanguagePublicationError, label="language export")
    if reader.text("config_name") != LANGUAGE_CONFIG_NAME:
        raise LanguagePublicationError("export declares a different configuration name")
    export = LanguageExport(
        export_root=export_root,
        config_name=LANGUAGE_CONFIG_NAME,
        snapshot_id=reader.text("snapshot_id"),
        model_config_fingerprint=reader.text("model_config_fingerprint"),
        library_name=reader.text("library_name"),
        library_version=reader.text("library_version"),
        files=reader.texts("files"),
        stats=_stats_from_payload(reader),
    )
    _validate_export_seal(export)
    return export


def _upload_item(export_root: Path, relative_path: str) -> UploadItem:
    path = export_root / relative_path
    if path.is_symlink() or not path.is_file():
        raise LanguagePublicationError(f"export file is missing or unsafe: {relative_path}")
    _require_contained_export_path(export_root, path)
    return UploadItem(
        relative_path=relative_path,
        size_bytes=path.stat().st_size,
        sha256=file_sha256(path),
    )


def _require_contained_export_path(export_root: Path, path: Path) -> None:
    if not path.resolve().is_relative_to(export_root.resolve()):
        raise LanguagePublicationError(f"export path has an unsafe parent directory: {path}")


def _validate_allowlist(relative_paths: Sequence[str]) -> None:
    prefix = f"{LANGUAGE_REMOTE_PREFIX}/"
    for relative_path in relative_paths:
        if not relative_path.startswith(prefix):
            raise LanguagePublicationError(
                f"upload plan may only contain {prefix} paths, found: {relative_path}"
            )
        if ".." in Path(relative_path).parts:
            raise LanguagePublicationError(f"upload path must not traverse: {relative_path}")


def build_language_upload_plan(
    export: LanguageExport,
    repo_id: str,
    *,
    confirm_repo: str,
) -> UploadPlan:
    """Build the exact additive upload plan for one validated export.

    The caller must repeat the repository identifier, and the resulting plan can
    only ever address ``language-v1`` paths, so publishing this configuration
    cannot disturb the default dataset.
    """
    if confirm_repo != repo_id:
        raise LanguagePublicationError(
            f"repository confirmation {confirm_repo!r} does not match {repo_id!r}"
        )
    relative_paths = (*export.files, LANGUAGE_STATS_PATH, LANGUAGE_MANIFEST_PATH)
    _validate_allowlist(relative_paths)
    _validate_export_seal(export)
    items = tuple(_upload_item(export.export_root, path) for path in sorted(relative_paths))
    return _identified_plan(repo_id, export.export_root, items)


def _identified_plan(repo_id: str, root: Path, items: tuple[UploadItem, ...]) -> UploadPlan:
    provisional = UploadPlan(repo_id=repo_id, data_root=str(root), files=items, identity_sha256="")
    identity = file_sha256_bytes(provisional.to_json().encode())
    return UploadPlan(repo_id=repo_id, data_root=str(root), files=items, identity_sha256=identity)


def language_config_yaml() -> str:
    """Return the additive dataset-card ``configs`` entry for this namespace."""
    return (
        f"  - config_name: {LANGUAGE_CONFIG_NAME}\n"
        f"    data_files:\n"
        f"      - split: train\n"
        f"        path: {LANGUAGE_DATA_PREFIX}/*.parquet\n"
    )


def render_language_card_section(export: LanguageExport) -> str:
    """Render the dataset-card section from validated, exported counts only."""
    stats = export.stats
    return f"""## Language annotations (`{LANGUAGE_CONFIG_NAME}`)

An additive configuration that labels the language of each OpenStreetMap
description value. The default configuration, its GeoParquet files, and schema
3 are unchanged.

**Counting unit.** One row per *description value*, not per polygon. An object
with a base `description` and two `description:<suffix>` values contributes
three rows.

| Measure | Value |
| --- | --- |
| Annotations | {stats.annotation_count} |
| Distinct OSM objects | {stats.object_count} |
| Base `description` values | {stats.base_description_count} |
| Localized `description:*` values | {stats.localized_description_count} |
| Detected | {stats.detected_count} |
| Uncertain | {stats.uncertain_count} |
| Non-linguistic | {stats.non_linguistic_count} |
| Distinct languages assigned | {stats.distinct_language_count} |

**Provenance.** Produced by `{export.library_name}` {export.library_version}
over input snapshot `{export.snapshot_id}` with detector configuration
`{export.model_config_fingerprint}`.

**Limitations.**

- `top_score`, `runner_up_score`, and `margin` are **raw detector scores, not
  calibrated probabilities**. They must not be read as confidence percentages.
- No accuracy has been measured on this dataset. The detector was selected for
  its documented suitability on short text, and has **not** been benchmarked
  here.
- Short values are frequently `uncertain` by design: a conservative minimum
  length, minimum score, and minimum margin are applied before any language is
  assigned.
- When the detector identifies mixed-language evidence, the value is reported
  as `uncertain` with reason `mixed_text`. Mixed-language detection is not
  guaranteed, especially for short values.
- Languages outside the detector's supported set cannot be assigned. Such
  values may be `uncertain` or may be misclassified as a supported language.
- Localized `description:<suffix>` keys are treated as opaque. A suffix is
  **not** assumed to be a language code, and the annotation reflects the text
  itself rather than the suffix.
- Text that contains no letters is labelled `non_linguistic`.

**Licensing and attribution.** The annotated text is OpenStreetMap data,
© OpenStreetMap contributors, available under the Open Database License (ODbL).
Language labels are derived annotations produced with
`{export.library_name}`, which is distributed under the Apache License 2.0.
"""


__all__ = [
    "EXPORT_BATCH_ROWS",
    "LANGUAGE_CONFIG_NAME",
    "LANGUAGE_DATA_PREFIX",
    "LANGUAGE_MANIFEST_PATH",
    "LANGUAGE_REMOTE_PREFIX",
    "LANGUAGE_STATS_PATH",
    "LanguageExport",
    "LanguagePublicationError",
    "LanguageStats",
    "build_language_upload_plan",
    "export_language_annotations",
    "language_config_yaml",
    "read_language_export",
    "render_language_card_section",
]
