"""Focused behavioral coverage for atomic publication-state persistence."""

from __future__ import annotations

import builtins
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

import osm_polygon_description_tag.publication.state as state
from osm_polygon_description_tag.publication.state import PublicationStateError


def test_atomic_write_json_creates_parents_and_stable_utf8_json(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    path = tmp_path / "nested" / "deeper" / "state.json"
    payload = {"z": "é", "a": {"number": 1}}
    write_encodings: list[object] = []
    open_modes: list[str] = []
    real_write_text = Path.write_text
    real_open = builtins.open

    def write_text(self: Path, data: str, *args: Any, **kwargs: Any) -> int:
        write_encodings.append(kwargs.get("encoding"))
        return real_write_text(self, data, *args, **kwargs)

    def open_file(file: Any, mode: str = "r", *args: Any, **kwargs: Any) -> Any:
        open_modes.append(mode)
        return real_open(file, mode, *args, **kwargs)

    monkeypatch.setattr(Path, "write_text", write_text)
    monkeypatch.setattr(builtins, "open", open_file)

    state._atomic_write_json(path, payload)

    assert path.read_bytes() == ('{\n  "a": {\n    "number": 1\n  },\n  "z": "é"\n}\n'.encode())
    assert write_encodings == ["utf-8"]
    assert "rb" in open_modes
    assert list(path.parent.glob(f".{path.name}.*.tmp")) == []


def test_read_publication_state_returns_default_and_reads_exact_encoding(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    assert state.read_publication_state(tmp_path) == {"schema_version": 1, "published": {}}
    path = tmp_path / state.PUBLICATION_STATE_FILENAME
    path.write_text('{"schema_version": 1, "published": {}}', encoding="utf-8")
    seen: list[object] = []
    real_read_text = Path.read_text

    def read_text(self: Path, *args: Any, **kwargs: Any) -> str:
        seen.append(kwargs.get("encoding"))
        return real_read_text(self, *args, **kwargs)

    monkeypatch.setattr(Path, "read_text", read_text)

    assert state.read_publication_state(tmp_path) == {"schema_version": 1, "published": {}}
    assert seen == ["utf-8"]


def test_write_publication_state_writes_complete_source_record(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    initial = {"schema_version": 1, "published": {"old.osm.pbf": {"remote_revision": "old"}}}
    monkeypatch.setattr(state, "read_publication_state", lambda _root: initial)
    writes: list[tuple[Path, dict[str, object]]] = []
    monkeypatch.setattr(
        state, "_atomic_write_json", lambda path, payload: writes.append((path, payload))
    )

    result = state._write_publication_state(
        tmp_path,
        source_name="new.osm.pbf",
        source_sha256="source-sha",
        output_sha256="output-sha",
        output_bytes=123,
        remote_revision="revision-1",
        artifact_identity="artifact-identity",
        completed_at="2026-08-22T10:00:00+00:00",
    )

    expected = {
        "schema_version": 1,
        "published": {
            "old.osm.pbf": {"remote_revision": "old"},
            "new.osm.pbf": {
                "source_sha256": "source-sha",
                "output_sha256": "output-sha",
                "output_bytes": 123,
                "remote_revision": "revision-1",
                "artifact_identity": "artifact-identity",
                "completed_at": "2026-08-22T10:00:00+00:00",
            },
        },
        "last_updated_at": "2026-08-22T10:00:00+00:00",
    }
    assert result == expected
    assert writes == [(tmp_path / state.PUBLICATION_STATE_FILENAME, expected)]


def test_write_publication_state_creates_missing_published_section(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    initial = {"schema_version": 1}
    monkeypatch.setattr(state, "read_publication_state", lambda _root: initial)
    monkeypatch.setattr(state, "_atomic_write_json", lambda *_args: None)

    result = state._write_publication_state(
        tmp_path,
        source_name="new.osm.pbf",
        source_sha256="s",
        output_sha256="o",
        output_bytes=1,
        remote_revision="r",
        artifact_identity="a",
        completed_at="now",
    )

    assert result["published"] == {
        "new.osm.pbf": {
            "source_sha256": "s",
            "output_sha256": "o",
            "output_bytes": 1,
            "remote_revision": "r",
            "artifact_identity": "a",
            "completed_at": "now",
        }
    }


def test_write_publication_state_rejects_unsupported_schema_exactly(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    monkeypatch.setattr(state, "read_publication_state", lambda _root: {"schema_version": 2})

    with pytest.raises(
        PublicationStateError,
        match=r"^unsupported publication state schema: 2$",
    ):
        state._write_publication_state(
            tmp_path,
            source_name="new.osm.pbf",
            source_sha256="s",
            output_sha256="o",
            output_bytes=1,
            remote_revision="r",
            artifact_identity="a",
            completed_at="now",
        )


def _metadata_files(tmp_path: Path) -> dict[str, Path]:
    paths = state._metadata_paths(tmp_path)
    for path in paths.values():
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(path.name.encode("utf-8"))
    return paths


def test_metadata_paths_use_exact_managed_artifact_names(tmp_path: Path) -> None:
    assert state._metadata_paths(tmp_path) == {
        "readme": tmp_path / "README.md",
        "stats": tmp_path / "stats.json",
        "h3_map": tmp_path / state.H3_MAP_ASSET_RELATIVE_PATH,
        "area_histogram": tmp_path / state.AREA_HISTOGRAM_ASSET_RELATIVE_PATH,
        "hero": tmp_path / state.DATASET_CARD_HERO_ASSET_RELATIVE_PATH,
    }


def test_metadata_files_exist_rejects_missing_files_and_symlinks(tmp_path: Path) -> None:
    paths = _metadata_files(tmp_path)
    assert state._metadata_files_exist(paths) is True

    paths["stats"].unlink()
    assert state._metadata_files_exist(paths) is False
    paths["stats"].write_bytes(b"stats")

    paths["stats"].unlink()
    paths["stats"].symlink_to(paths["readme"])
    assert state._metadata_files_exist(paths) is False


@pytest.mark.parametrize(
    "state_payload",
    [{}, {"metadata": {"identity_sha256": "wrong"}}],
)
def test_metadata_state_matches_rejects_missing_or_wrong_identity(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    state_payload: dict[str, object],
) -> None:
    plan = SimpleNamespace(identity_sha256="expected")
    monkeypatch.setattr(state, "read_publication_state", lambda _root: state_payload)

    assert state._metadata_state_matches(tmp_path, plan) is False


def test_metadata_state_matches_rejects_missing_files(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    plan = SimpleNamespace(identity_sha256="expected")
    monkeypatch.setattr(
        state,
        "read_publication_state",
        lambda _root: {"metadata": {"identity_sha256": "expected"}},
    )
    monkeypatch.setattr(state, "_metadata_files_exist", lambda _paths: False)

    assert state._metadata_state_matches(tmp_path, plan) is False


def test_metadata_state_matches_passes_metadata_dict_to_identity_checker(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    metadata = {"identity_sha256": "expected", "readme_sha256": "hash"}
    plan = SimpleNamespace(identity_sha256="expected")
    monkeypatch.setattr(state, "read_publication_state", lambda _root: {"metadata": metadata})
    monkeypatch.setattr(state, "_metadata_files_exist", lambda _paths: True)
    seen: list[dict[str, object]] = []

    def identity_matches(actual: dict[str, object], _paths: dict[str, Path]) -> bool:
        seen.append(actual)
        return True

    monkeypatch.setattr(state, "_metadata_identity_matches", identity_matches)

    assert state._metadata_state_matches(tmp_path, plan) is True
    assert seen == [metadata]


def test_write_metadata_state_writes_required_and_optional_fields(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    initial = {"schema_version": 1, "published": {}}
    monkeypatch.setattr(state, "_current_state", lambda _root: initial)
    writes: list[tuple[Path, dict[str, object]]] = []
    monkeypatch.setattr(
        state, "_atomic_write_json", lambda path, payload: writes.append((path, payload))
    )

    result = state._write_metadata_state(
        tmp_path,
        identity_sha256="identity",
        readme_sha256="readme",
        stats_sha256="stats",
        readme_size_bytes=10,
        stats_size_bytes=20,
        h3_map_sha256="h3",
        h3_map_size_bytes=30,
        area_histogram_sha256="area",
        area_histogram_size_bytes=40,
        dataset_card_hero_sha256="hero",
        dataset_card_hero_size_bytes=50,
        verified_revision="revision",
        completed_at="now",
    )

    expected_metadata = {
        "identity_sha256": "identity",
        "readme_sha256": "readme",
        "stats_sha256": "stats",
        "readme_size_bytes": 10,
        "stats_size_bytes": 20,
        "verified_revision": "revision",
        "completed_at": "now",
        "h3_map_sha256": "h3",
        "h3_map_size_bytes": 30,
        "area_histogram_sha256": "area",
        "area_histogram_size_bytes": 40,
        "dataset_card_hero_sha256": "hero",
        "dataset_card_hero_size_bytes": 50,
    }
    expected = {
        "schema_version": 1,
        "published": {},
        "metadata": expected_metadata,
        "last_updated_at": "now",
    }
    assert result == expected
    assert writes == [(tmp_path / state.PUBLICATION_STATE_FILENAME, expected)]


def test_current_state_rejects_unsupported_schema_exactly(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    monkeypatch.setattr(state, "read_publication_state", lambda _root: {"schema_version": 3})

    with pytest.raises(PublicationStateError, match=r"^unsupported publication state schema: 3$"):
        state._current_state(tmp_path)


def test_cast_dict_validates_runtime_shape_and_preserves_mapping() -> None:
    value = {"key": "value"}
    assert state.cast_dict(value) is value

    with pytest.raises(PublicationStateError, match=r"^expected dict, got list$"):
        state.cast_dict([])
