"""Characterisation of the immutable snapshot identity and its JSON document.

The snapshot id and document digest were recorded before the manifest model
was separated from the snapshot discovery and preparation code. Constructing
the manifest with the recorded id also proves the canonical identity still
matches the payload it hashes.
"""

from __future__ import annotations

import hashlib

from osm_polygon_description_tag.dataset.languages.models import (
    LanguagePolicy,
    cascade_model_identity,
)
from osm_polygon_description_tag.dataset.languages.snapshot import (
    SnapshotManifest,
    SourceFileSnapshot,
)

_SNAPSHOT_ID = "fc6f6ff00141fa6be2578a5dbc26104dd4f213a0c1e57447a57bb04d8a480b65"
_JSON_SHA256 = "2d203548dd0e1a6d7e4ddad8d295c7c48e36cadf58775904eebcf428ff7ebec8"
_CONFIG_FINGERPRINT = "1d6f31e245a922d89d6d341f24cf0db2304160b2b7ce4f5d8eb890eef148c9a7"


def _manifest() -> SnapshotManifest:
    files = (
        SourceFileSnapshot(
            relative_path="region-a.parquet",
            size_bytes=1234,
            sha256="a" * 64,
            schema_version=3,
            schema_fingerprint="b" * 64,
            row_count=7,
        ),
        SourceFileSnapshot(
            relative_path="region-b/part.parquet",
            size_bytes=99,
            sha256="c" * 64,
            schema_version=3,
            schema_fingerprint="b" * 64,
            row_count=0,
        ),
    )
    return SnapshotManifest(
        snapshot_id=_SNAPSHOT_ID,
        snapshot_schema_version=1,
        source_schema_version=3,
        source_files=files,
        model_identity=cascade_model_identity(LanguagePolicy()),
        code_fingerprint="d" * 64,
        lock_fingerprint="e" * 64,
    )


def test_manifest_identity_and_json_document_are_pinned() -> None:
    manifest = _manifest()

    assert manifest.snapshot_id == _SNAPSHOT_ID
    assert manifest.model_config_fingerprint == _CONFIG_FINGERPRINT
    assert hashlib.sha256(manifest.to_json().encode("utf-8")).hexdigest() == _JSON_SHA256


def test_manifest_round_trips_through_its_recorded_payload() -> None:
    manifest = _manifest()

    assert SnapshotManifest.from_payload(manifest.to_payload()) == manifest
    assert manifest.source_file("region-b/part.parquet").row_count == 0
