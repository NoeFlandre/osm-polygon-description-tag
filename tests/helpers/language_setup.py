"""Canonical synthetic source and run builders for language dataset tests."""

import json
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pyarrow.parquet as pq
from shapely.geometry import Polygon

from osm_polygon_description_tag.dataset.languages.checkpoint import shard_paths
from osm_polygon_description_tag.dataset.languages.detector import LanguageDetectionCallable
from osm_polygon_description_tag.dataset.languages.models import LanguageModelIdentity
from osm_polygon_description_tag.dataset.languages.snapshot import (
    SnapshotManifest,
    prepare_snapshot,
)
from osm_polygon_description_tag.dataset.languages.worker import process_shard
from osm_polygon_description_tag.dataset.sentences.splitter import SentenceSplitter
from osm_polygon_description_tag.dataset.storage import write_geoparquet
from tests.conftest import make_record_dict
from tests.helpers.parquet import write_description_shard

LanguageTags = Callable[[int], dict[str, str]]
DEFAULT_SHARD = "region.parquet"


def rewrite_snapshot(run: Path, mutate: Callable[[dict[str, Any]], None]) -> None:
    """Apply one targeted mutation to the immutable snapshot JSON fixture."""
    path = run / "snapshot.json"
    payload = json.loads(path.read_text(encoding="utf-8"))
    mutate(payload)
    path.write_text(json.dumps(payload), encoding="utf-8")


def tamper_checkpoint(run: Path, **changes: object) -> None:
    """Change selected fields in the standard language checkpoint fixture."""
    path = shard_paths(run, DEFAULT_SHARD).checkpoint
    payload = json.loads(path.read_text(encoding="utf-8"))
    payload.update(changes)
    path.write_text(json.dumps(payload), encoding="utf-8")


class LanguageRunSetup:
    """Build the stable source shard, snapshot, and run shared by tests."""

    def write_snapshot_source(self, path: Path, *, text: str = "A synthetic description") -> None:
        """Write the one-row source used by snapshot identity tests."""
        record = make_record_dict(Polygon([(0, 0), (0, 1), (1, 1), (1, 0)]), {"description": text})
        path.parent.mkdir(parents=True, exist_ok=True)
        write_geoparquet(iter([record]), path, batch_size=1)

    def prepare_snapshot(
        self,
        source: Path,
        run: Path,
        *,
        model_identity: LanguageModelIdentity | None = None,
    ) -> SnapshotManifest:
        """Prepare a snapshot with the fixed test code and lock fingerprints."""
        return prepare_snapshot(
            source,
            run,
            code_fingerprint="a" * 64,
            lock_fingerprint="b" * 64,
            model_identity=model_identity,
        )

    def prepared_snapshot(
        self,
        root: Path,
        *,
        model_identity: LanguageModelIdentity | None = None,
    ) -> tuple[Path, Path, SnapshotManifest]:
        """Create the common one-row ``region.parquet`` snapshot run."""
        source = root / "source"
        self.write_snapshot_source(source / DEFAULT_SHARD)
        run = root / "run"
        return source, run, self.prepare_snapshot(source, run, model_identity=model_identity)

    def prepare_run(
        self,
        root: Path,
        *,
        count: int = 12,
        row_group_size: int = 4,
        tags: LanguageTags | None = None,
        model_identity: LanguageModelIdentity | None = None,
        normalize_row_groups: bool = False,
    ) -> tuple[Path, Path, SnapshotManifest]:
        """Write one canonical shard and prepare its snapshot and run directory."""
        source = root / "source"
        source_path = source / DEFAULT_SHARD
        if tags is None:
            write_description_shard(source_path, count, batch_size=row_group_size)
        else:
            write_description_shard(source_path, count, batch_size=row_group_size, tags=tags)
        if normalize_row_groups:
            table = pq.read_table(source_path)
            pq.write_table(table, source_path, row_group_size=row_group_size, compression="zstd")
        run = root / "run"
        snapshot = self.prepare_snapshot(source, run, model_identity=model_identity)
        return source, run, snapshot

    def prepare_complete_run(
        self,
        root: Path,
        *,
        detector: LanguageDetectionCallable,
        splitter: SentenceSplitter,
        count: int = 8,
        batch_size: int = 4,
    ) -> tuple[Path, Path, SnapshotManifest]:
        """Create and process the canonical complete shard used by validation tests."""
        source, run, snapshot = self.prepare_run(root, count=count, row_group_size=batch_size)
        process_shard(
            run,
            source,
            DEFAULT_SHARD,
            detector=detector,
            splitter=splitter,
            snapshot=snapshot,
            batch_size=batch_size,
        )
        return source, run, snapshot
