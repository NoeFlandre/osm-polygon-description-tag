"""Immutable snapshot identity, strict payloads, and path containment."""

import hashlib
import json
import threading
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any

import pyarrow as pa
import pyarrow.parquet as pq
import pytest
from shapely.geometry import Polygon

from osm_polygon_description_tag.dataset.languages import snapshot as snapshot_module
from osm_polygon_description_tag.dataset.languages.checkpoint import WorkerBusyError
from osm_polygon_description_tag.dataset.languages.models import (
    LanguagePolicy,
    cascade_model_identity,
    language_model_identity,
)
from osm_polygon_description_tag.dataset.languages.snapshot import (
    SnapshotError,
    SnapshotManifest,
    SourceFileSnapshot,
    fingerprint_lockfile,
    fingerprint_project_source,
    inspect_source_file,
    prepare_snapshot,
    read_snapshot,
    source_path_for,
    verify_all_source_files,
    verify_project_identity,
    verify_source_file,
)
from osm_polygon_description_tag.dataset.schema import SCHEMA
from osm_polygon_description_tag.storage import write_geoparquet
from tests.conftest import make_record_dict
from tests.helpers.messages import exactly


def _write_source(path: Path, *, text: str = "A synthetic description") -> None:
    record = make_record_dict(Polygon([(0, 0), (0, 1), (1, 1), (1, 0)]), {"description": text})
    path.parent.mkdir(parents=True, exist_ok=True)
    write_geoparquet(iter([record]), path, batch_size=1)


def _prepare(source: Path, run: Path) -> SnapshotManifest:
    return prepare_snapshot(
        source,
        run,
        code_fingerprint="a" * 64,
        lock_fingerprint="b" * 64,
        model_identity=language_model_identity(LanguagePolicy()),
    )


def _prepared(tmp_path: Path) -> tuple[Path, Path, SnapshotManifest]:
    source = tmp_path / "source"
    _write_source(source / "region.parquet")
    run = tmp_path / "run"
    return source, run, _prepare(source, run)


def _rewrite(run: Path, mutate: Callable[[dict[str, Any]], None]) -> None:
    path = run / "snapshot.json"
    payload = json.loads(path.read_text(encoding="utf-8"))
    mutate(payload)
    path.write_text(json.dumps(payload), encoding="utf-8")


def test_snapshot_is_immutable_and_portable_across_source_root_paths(tmp_path: Path) -> None:
    first_source = tmp_path / "first" / "data"
    _write_source(first_source / "region.parquet")

    first = _prepare(first_source, tmp_path / "run")
    same = _prepare(first_source, tmp_path / "run")

    second_source = tmp_path / "second" / "data"
    _write_source(second_source / "region.parquet")
    second = _prepare(second_source, tmp_path / "second-run")

    assert first == same
    assert first.snapshot_id == second.snapshot_id
    payload = json.loads((tmp_path / "run" / "snapshot.json").read_text(encoding="utf-8"))
    assert "source_root" not in payload
    assert payload["source_files"][0]["relative_path"] == "region.parquet"
    assert payload["source_files"][0]["schema_version"] == 3
    assert read_snapshot(tmp_path / "run") == first
    assert first.model_config_fingerprint == first.model_identity.config_fingerprint


def test_snapshot_round_trips_the_cascade_fallback_identity(tmp_path: Path) -> None:
    source = tmp_path / "source"
    _write_source(source / "region.parquet")
    run = tmp_path / "run"
    identity = cascade_model_identity(LanguagePolicy(), language_scope=("eng", "fra"))

    prepare_snapshot(
        source,
        run,
        code_fingerprint="a" * 64,
        lock_fingerprint="b" * 64,
        model_identity=identity,
    )

    payload = json.loads((run / "snapshot.json").read_text(encoding="utf-8"))
    assert payload["model_identity"]["detector_name"] == "lingua+glotlid-v3-fallback"
    assert payload["model_identity"]["model_filename"] == "model_v3.bin"
    assert read_snapshot(run).model_identity == identity


def test_snapshot_rejects_source_drift_instead_of_overwriting_identity(tmp_path: Path) -> None:
    source, run, snapshot = _prepared(tmp_path)

    _write_source(source / "region.parquet", text="Changed synthetic description")

    with pytest.raises(SnapshotError, match="immutable snapshot"):
        _prepare(source, run)
    with pytest.raises(SnapshotError, match="does not match snapshot"):
        verify_source_file(snapshot, source, "region.parquet")
    with pytest.raises(
        SnapshotError, match=exactly("source directory files do not match immutable snapshot")
    ):
        verify_all_source_files(snapshot, source)


def test_verification_accepts_an_unchanged_source_tree(tmp_path: Path) -> None:
    source, _, snapshot = _prepared(tmp_path)

    assert verify_source_file(snapshot, source, "region.parquet") == snapshot.source_files[0]
    assert verify_all_source_files(snapshot, source) == snapshot.source_files


def test_verify_all_source_files_rejects_an_extra_parquet(tmp_path: Path) -> None:
    source, _, snapshot = _prepared(tmp_path)
    _write_source(source / "extra.parquet", text="Another synthetic description")

    with pytest.raises(
        SnapshotError, match=exactly("source directory files do not match immutable snapshot")
    ):
        verify_all_source_files(snapshot, source)


def test_snapshot_rejects_non_schema_three_sources(tmp_path: Path) -> None:
    source = tmp_path / "source"
    source.mkdir()
    pq.write_table(pa.table({"source_pbf": ["region.osm.pbf"]}), source / "invalid.parquet")

    with pytest.raises(SnapshotError, match="schema"):
        _prepare(source, tmp_path / "run")


def test_snapshot_rejects_a_field_type_change_in_a_current_schema(tmp_path: Path) -> None:
    source = tmp_path / "source"
    _write_source(source / "region.parquet")
    table = pq.read_table(source / "region.parquet")
    altered = table.set_column(
        table.schema.get_field_index("osm_id"),
        "osm_id",
        table.column("osm_id").cast(pa.int32()),
    )
    pq.write_table(altered, source / "region.parquet")

    with pytest.raises(SnapshotError, match="field mismatch"):
        _prepare(source, tmp_path / "run")


def test_snapshot_rejects_output_inside_the_immutable_source(tmp_path: Path) -> None:
    source = tmp_path / "source"
    _write_source(source / "region.parquet")

    with pytest.raises(
        SnapshotError, match=exactly("run output must be outside the immutable source directory")
    ):
        _prepare(source, source / "run")


def test_snapshot_rejects_a_run_directory_containing_the_source(tmp_path: Path) -> None:
    parent = tmp_path / "parent"
    source = parent / "source"
    _write_source(source / "region.parquet")

    with pytest.raises(
        SnapshotError, match=exactly("run output must not contain the immutable source directory")
    ):
        _prepare(source, parent)


def test_snapshot_rejects_symlinked_sources(tmp_path: Path) -> None:
    real_source = tmp_path / "real"
    _write_source(real_source / "region.parquet")

    linked = tmp_path / "linked"
    linked.mkdir()
    linked.joinpath("region.parquet").symlink_to(real_source / "region.parquet")
    with pytest.raises(SnapshotError, match="symlink"):
        _prepare(linked, tmp_path / "linked-run")

    linked_root = tmp_path / "linked-root"
    linked_root.symlink_to(real_source)
    with pytest.raises(SnapshotError, match="must not be a symlink"):
        _prepare(linked_root, tmp_path / "root-run")


def test_snapshot_rejects_an_unusable_run_directory(tmp_path: Path) -> None:
    source = tmp_path / "source"
    _write_source(source / "region.parquet")

    occupied = tmp_path / "occupied"
    occupied.mkdir()
    occupied.joinpath("stray.txt").write_text("stray", encoding="utf-8")
    with pytest.raises(
        SnapshotError, match=exactly("cannot initialize snapshot in a non-empty run directory")
    ):
        _prepare(source, occupied)

    as_file = tmp_path / "as-file"
    as_file.write_text("not a directory", encoding="utf-8")
    with pytest.raises(SnapshotError, match="not a regular directory"):
        _prepare(source, as_file)

    linked_run = tmp_path / "linked-run"
    linked_run.symlink_to(occupied)
    with pytest.raises(SnapshotError, match="not a regular directory"):
        _prepare(source, linked_run)


def test_snapshot_rejects_invalid_fingerprint_arguments(tmp_path: Path) -> None:
    source = tmp_path / "source"
    _write_source(source / "region.parquet")

    with pytest.raises(SnapshotError, match="code fingerprint"):
        prepare_snapshot(
            source, tmp_path / "run", code_fingerprint="short", lock_fingerprint="b" * 64
        )
    with pytest.raises(SnapshotError, match="lock fingerprint"):
        prepare_snapshot(
            source, tmp_path / "run", code_fingerprint="a" * 64, lock_fingerprint="B" * 64
        )


def test_snapshot_rejects_a_non_identity_model_argument(tmp_path: Path) -> None:
    source = tmp_path / "source"
    _write_source(source / "region.parquet")

    with pytest.raises(TypeError, match=exactly("model_identity must be a LanguageModelIdentity")):
        prepare_snapshot(
            source,
            tmp_path / "run",
            code_fingerprint="a" * 64,
            lock_fingerprint="b" * 64,
            model_identity=LanguagePolicy(),  # type: ignore[arg-type]
        )


def test_snapshot_defaults_to_the_configured_policy_identity(tmp_path: Path) -> None:
    source = tmp_path / "source"
    _write_source(source / "region.parquet")

    snapshot = prepare_snapshot(
        source,
        tmp_path / "run",
        code_fingerprint="a" * 64,
        lock_fingerprint="b" * 64,
        policy=LanguagePolicy(min_alphabetic_chars=9),
    )

    assert snapshot.model_identity.policy.min_alphabetic_chars == 9


def test_reading_a_snapshot_rejects_unreadable_and_non_object_documents(tmp_path: Path) -> None:
    run = tmp_path / "run"
    run.mkdir()
    with pytest.raises(SnapshotError, match="cannot read snapshot"):
        read_snapshot(run)

    (run / "snapshot.json").write_text("{not-json", encoding="utf-8")
    with pytest.raises(SnapshotError, match="cannot read snapshot"):
        read_snapshot(run)

    (run / "snapshot.json").write_text("[]", encoding="utf-8")
    with pytest.raises(SnapshotError, match="must be an object"):
        read_snapshot(run)


def test_read_snapshot_rejects_a_valid_symlinked_manifest(tmp_path: Path) -> None:
    _, run, _ = _prepared(tmp_path)
    path = run / "snapshot.json"
    target = tmp_path / "external-snapshot.json"
    target.write_bytes(path.read_bytes())
    path.unlink()
    path.symlink_to(target)

    with pytest.raises(SnapshotError, match="snapshot must not be a symlink"):
        read_snapshot(run)


def test_prepare_snapshot_rejects_a_valid_symlinked_manifest(tmp_path: Path) -> None:
    source, run, _ = _prepared(tmp_path)
    path = run / "snapshot.json"
    target = tmp_path / "external-snapshot.json"
    target.write_bytes(path.read_bytes())
    path.unlink()
    path.symlink_to(target)

    with pytest.raises(SnapshotError, match="snapshot must not be a symlink"):
        _prepare(source, run)


def test_concurrent_snapshot_preparation_cannot_replace_the_manifest(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    first_source = tmp_path / "first-source"
    second_source = tmp_path / "second-source"
    _write_source(first_source / "region.parquet", text="first")
    _write_source(second_source / "region.parquet", text="second")
    run = tmp_path / "run"
    write_started = threading.Event()
    release_write = threading.Event()
    original_write = snapshot_module.atomic_write_bytes
    write_count = 0

    def delayed_write(path: Path, content: bytes) -> None:
        nonlocal write_count
        write_count += 1
        if write_count == 1:
            write_started.set()
            if not release_write.wait(timeout=5):
                raise AssertionError("timed out waiting to release the first snapshot write")
        original_write(path, content)

    monkeypatch.setattr(snapshot_module, "atomic_write_bytes", delayed_write)
    with ThreadPoolExecutor(max_workers=2) as workers:
        first = workers.submit(_prepare, first_source, run)
        assert write_started.wait(timeout=5)
        second = workers.submit(_prepare, second_source, run)
        try:
            with pytest.raises(WorkerBusyError, match="already locked"):
                second.result(timeout=5)
        finally:
            release_write.set()
        first_snapshot = first.result(timeout=5)

    assert read_snapshot(run) == first_snapshot


@pytest.mark.parametrize(
    ("key", "value", "message"),
    [
        ("snapshot_id", 12345, "must be a string"),
        ("snapshot_schema_version", "1", "must be an integer"),
        ("source_schema_version", 3.0, "must be an integer"),
        ("source_files", {}, "must be a list"),
        ("model_identity", [], "must be an object"),
        ("code_fingerprint", None, "must be a string"),
    ],
)
def test_snapshot_payload_rejects_wrongly_typed_fields(
    tmp_path: Path, key: str, value: object, message: str
) -> None:
    _, run, _ = _prepared(tmp_path)
    _rewrite(run, lambda payload: payload.__setitem__(key, value))

    with pytest.raises(SnapshotError, match=message):
        read_snapshot(run)


def test_snapshot_payload_rejects_missing_fields(tmp_path: Path) -> None:
    _, run, _ = _prepared(tmp_path)
    _rewrite(run, lambda payload: payload.pop("lock_fingerprint"))

    with pytest.raises(SnapshotError, match="missing lock_fingerprint"):
        read_snapshot(run)


@pytest.mark.parametrize(
    ("key", "value", "message"),
    [
        ("row_count", True, "must be an integer"),
        ("size_bytes", 1.5, "must be an integer"),
        ("relative_path", 7, "must be a string"),
        ("sha256", None, "must be a string"),
    ],
)
def test_source_file_payload_rejects_coercible_but_wrong_types(
    tmp_path: Path, key: str, value: object, message: str
) -> None:
    _, run, _ = _prepared(tmp_path)
    _rewrite(run, lambda payload: payload["source_files"][0].__setitem__(key, value))

    with pytest.raises(SnapshotError, match=message):
        read_snapshot(run)


def test_source_file_payload_rejects_non_object_entries(tmp_path: Path) -> None:
    _, run, _ = _prepared(tmp_path)
    _rewrite(run, lambda payload: payload.__setitem__("source_files", ["region.parquet"]))

    with pytest.raises(SnapshotError, match="source file payload must be an object"):
        read_snapshot(run)


def test_snapshot_payload_rejects_a_tampered_identity(tmp_path: Path) -> None:
    _, run, _ = _prepared(tmp_path)
    _rewrite(run, lambda payload: payload["source_files"][0].__setitem__("row_count", 99))

    with pytest.raises(
        SnapshotError, match=exactly("snapshot id does not match canonical content")
    ):
        read_snapshot(run)


@pytest.mark.parametrize(
    ("key", "value", "message"),
    [
        ("library_version", "9.9.9", "does not match derived value"),
        ("config_fingerprint", "c" * 64, "does not match derived value"),
        ("binary_artifact_hash", "d" * 64, "binary artifact hash must remain unset"),
    ],
)
def test_model_identity_payload_rejects_unverifiable_claims(
    tmp_path: Path, key: str, value: object, message: str
) -> None:
    _, run, _ = _prepared(tmp_path)
    _rewrite(run, lambda payload: payload["model_identity"].__setitem__(key, value))

    with pytest.raises(SnapshotError, match=message):
        read_snapshot(run)


def test_model_identity_payload_rejects_malformed_policies_and_scopes(tmp_path: Path) -> None:
    _, run, _ = _prepared(tmp_path)
    _rewrite(
        run, lambda payload: payload["model_identity"]["policy"].__setitem__("tie_epsilon", 5.0)
    )
    with pytest.raises(SnapshotError, match="invalid snapshot policy"):
        read_snapshot(run)

    _, second_run, _ = _prepared(tmp_path / "second")
    _rewrite(
        second_run,
        lambda payload: payload["model_identity"].__setitem__("language_scope", [7]),
    )
    with pytest.raises(SnapshotError, match="must contain only strings"):
        read_snapshot(second_run)


def test_manifest_rejects_unsorted_or_duplicated_source_files() -> None:
    identity = language_model_identity(LanguagePolicy())
    first = SourceFileSnapshot("a.parquet", 1, "a" * 64, 3, "b" * 64, 1)
    second = SourceFileSnapshot("b.parquet", 1, "c" * 64, 3, "d" * 64, 1)

    with pytest.raises(
        SnapshotError, match=exactly("source files must be sorted by relative path")
    ):
        SnapshotManifest("e" * 64, 1, 3, (second, first), identity, "f" * 64, "0" * 64)
    with pytest.raises(
        SnapshotError, match=exactly("source files must not contain duplicate relative paths")
    ):
        SnapshotManifest("e" * 64, 1, 3, (first, first), identity, "f" * 64, "0" * 64)


def test_manifest_rejects_unsupported_versions_and_identities() -> None:
    identity = language_model_identity(LanguagePolicy())

    with pytest.raises(SnapshotError, match="unsupported snapshot schema version"):
        SnapshotManifest("e" * 64, 2, 3, (), identity, "f" * 64, "0" * 64)
    with pytest.raises(SnapshotError, match="source schema version must be 3"):
        SnapshotManifest("e" * 64, 1, 2, (), identity, "f" * 64, "0" * 64)
    with pytest.raises(TypeError, match=exactly("model_identity must be a LanguageModelIdentity")):
        SnapshotManifest("e" * 64, 1, 3, (), "identity", "f" * 64, "0" * 64)  # type: ignore[arg-type]


@pytest.mark.parametrize(
    ("kwargs", "message"),
    [
        ({"relative_path": "/abs.parquet"}, "must be relative"),
        ({"relative_path": "../escape.parquet"}, "must not contain traversal"),
        ({"size_bytes": -1}, "size_bytes must be a non-negative integer"),
        ({"size_bytes": True}, "size_bytes must be a non-negative integer"),
        ({"sha256": "nothex"}, "sha256 must be a lowercase"),
        ({"schema_version": 2}, "source schema version must be 3"),
        ({"row_count": -5}, "row_count must be a non-negative integer"),
    ],
)
def test_source_file_snapshot_validates_its_fields(kwargs: dict[str, object], message: str) -> None:
    defaults: dict[str, object] = {
        "relative_path": "region.parquet",
        "size_bytes": 10,
        "sha256": "a" * 64,
        "schema_version": 3,
        "schema_fingerprint": "b" * 64,
        "row_count": 1,
    }

    with pytest.raises(SnapshotError, match=message):
        SourceFileSnapshot(**{**defaults, **kwargs})  # type: ignore[arg-type]


def test_source_lookup_rejects_unknown_and_unsafe_paths(tmp_path: Path) -> None:
    source, _, snapshot = _prepared(tmp_path)

    with pytest.raises(SnapshotError, match="not in snapshot"):
        snapshot.source_file("missing.parquet")
    with pytest.raises(SnapshotError, match="must not contain traversal"):
        source_path_for(snapshot, source, "../region.parquet")
    with pytest.raises(SnapshotError, match="must be relative"):
        source_path_for(snapshot, source, "/region.parquet")
    with pytest.raises(SnapshotError, match="source path must be a non-empty relative path"):
        snapshot.source_file(7)  # type: ignore[arg-type]


def test_source_path_for_reports_a_removed_file(tmp_path: Path) -> None:
    source, _, snapshot = _prepared(tmp_path)
    (source / "region.parquet").unlink()

    with pytest.raises(SnapshotError, match="source file is missing"):
        source_path_for(snapshot, source, "region.parquet")


def test_source_path_for_rejects_a_replaced_symlink(tmp_path: Path) -> None:
    source, _, snapshot = _prepared(tmp_path)
    outside = tmp_path / "outside.parquet"
    (source / "region.parquet").rename(outside)
    (source / "region.parquet").symlink_to(outside)

    with pytest.raises(SnapshotError, match="escapes or uses a symlink"):
        source_path_for(snapshot, source, "region.parquet")


def test_inspect_source_file_rejects_unreadable_parquet(tmp_path: Path) -> None:
    source = tmp_path / "source"
    source.mkdir()
    broken = source / "broken.parquet"
    broken.write_bytes(b"not parquet")

    with pytest.raises(SnapshotError, match="cannot inspect source Parquet"):
        inspect_source_file(source, broken)

    with pytest.raises(SnapshotError, match="must be a regular non-symlink file"):
        inspect_source_file(source, source / "missing.parquet")


def test_inspect_source_file_rejects_a_path_outside_the_root(tmp_path: Path) -> None:
    source = tmp_path / "source"
    source.mkdir()
    other = tmp_path / "other"
    _write_source(other / "region.parquet")

    with pytest.raises(SnapshotError, match="escapes source directory"):
        inspect_source_file(source, other / "region.parquet")


def test_resolving_a_missing_source_directory_is_reported(tmp_path: Path) -> None:
    with pytest.raises(SnapshotError, match="cannot resolve source directory"):
        inspect_source_file(tmp_path / "absent", tmp_path / "absent" / "region.parquet")

    as_file = tmp_path / "file"
    as_file.write_text("x", encoding="utf-8")
    with pytest.raises(SnapshotError, match="is not a directory"):
        inspect_source_file(as_file, as_file)


def test_project_identity_rejects_a_non_manifest_before_reading_project(tmp_path: Path) -> None:
    with pytest.raises(TypeError) as caught:
        verify_project_identity(object(), tmp_path / "absent-project")  # type: ignore[arg-type]
    assert str(caught.value) == "snapshot must be a SnapshotManifest"


def test_project_fingerprint_refuses_a_file_removed_after_discovery(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    source = tmp_path / "src"
    source.mkdir()
    path = source / "vanishing.py"
    path.write_text("VALUE = 1\n", encoding="utf-8")
    original = Path.is_file
    removed = False

    def is_file(candidate: Path) -> bool:
        nonlocal removed
        result = original(candidate)
        if candidate == path and result and not removed:
            # Model a concurrent deletion after discovery, before hashing.
            candidate.unlink()
            removed = True
        return result

    monkeypatch.setattr(Path, "is_file", is_file)
    with pytest.raises(SnapshotError) as caught:
        fingerprint_project_source(tmp_path)
    assert removed
    assert str(caught.value) == f"fingerprint input must be a regular file: {path}"


def test_project_source_and_lock_fingerprints_change_deterministically(tmp_path: Path) -> None:
    project = tmp_path / "project"
    source_file = project / "src" / "example.py"
    source_file.parent.mkdir(parents=True)
    source_file.write_text("VALUE = 1\n", encoding="utf-8")
    lock = project / "uv.lock"
    lock.write_text("lock = 1\n", encoding="utf-8")

    source_fingerprint = fingerprint_project_source(project)
    lock_digest = fingerprint_lockfile(project)
    assert source_fingerprint == fingerprint_project_source(project)
    assert lock_digest == fingerprint_lockfile(project)

    source_file.write_text("VALUE = 2\n", encoding="utf-8")
    assert fingerprint_project_source(project) != source_fingerprint
    lock.write_text("lock = 2\n", encoding="utf-8")
    assert fingerprint_lockfile(project) != lock_digest


def test_project_source_fingerprint_ignores_import_caches_but_includes_package_data(
    tmp_path: Path,
) -> None:
    project = tmp_path / "project"
    source = project / "src"
    source.mkdir(parents=True)
    (source / "example.py").write_text("VALUE = 1\n", encoding="utf-8")
    baseline = fingerprint_project_source(project)

    cache = source / "__pycache__"
    cache.mkdir()
    (cache / "example.cpython-312.pyc").write_bytes(b"machine-specific bytecode")
    (source / "generated.pyc").write_bytes(b"machine-specific bytecode")

    assert fingerprint_project_source(project) == baseline

    package_data = source / "package" / "config.json"
    package_data.parent.mkdir()
    package_data.write_text('{"value": 1}\n', encoding="utf-8")
    with_data = fingerprint_project_source(project)
    assert with_data != baseline

    package_data.write_text('{"value": 2}\n', encoding="utf-8")
    assert fingerprint_project_source(project) != with_data


def test_project_identity_verification_rejects_code_or_lock_drift(tmp_path: Path) -> None:
    project = tmp_path / "project"
    source_code = project / "src" / "example.py"
    source_code.parent.mkdir(parents=True)
    source_code.write_text("VALUE = 1\n", encoding="utf-8")
    lock = project / "uv.lock"
    lock.write_text("lock = 1\n", encoding="utf-8")
    source = tmp_path / "dataset" / "source"
    _write_source(source / "region.parquet")
    run = tmp_path / "dataset" / "run"
    snapshot = prepare_snapshot(
        source,
        run,
        code_fingerprint=fingerprint_project_source(project),
        lock_fingerprint=fingerprint_lockfile(project),
    )

    verify_project_identity(snapshot, project)

    source_code.write_text("VALUE = 2\n", encoding="utf-8")
    with pytest.raises(
        SnapshotError, match=exactly("project source does not match snapshot code fingerprint")
    ):
        verify_project_identity(snapshot, project)

    source_code.write_text("VALUE = 1\n", encoding="utf-8")
    lock.write_text("lock = 2\n", encoding="utf-8")
    with pytest.raises(
        SnapshotError, match=exactly("project lockfile does not match snapshot lock fingerprint")
    ):
        verify_project_identity(snapshot, project)


def test_project_fingerprints_reject_missing_or_symlinked_inputs(tmp_path: Path) -> None:
    project = tmp_path / "project"
    project.mkdir()

    with pytest.raises(SnapshotError, match="project source directory is missing"):
        fingerprint_project_source(project)
    with pytest.raises(SnapshotError, match="uv.lock is missing"):
        fingerprint_lockfile(project)

    real_src = tmp_path / "real-src"
    real_src.mkdir()
    (project / "src").symlink_to(real_src)
    with pytest.raises(SnapshotError, match="project source directory is missing"):
        fingerprint_project_source(project)

    real_lock = tmp_path / "real.lock"
    real_lock.write_text("lock\n", encoding="utf-8")
    (project / "uv.lock").symlink_to(real_lock)
    with pytest.raises(SnapshotError, match="uv.lock is missing or is a symlink"):
        fingerprint_lockfile(project)


def test_project_source_fingerprint_rejects_symlinked_files(tmp_path: Path) -> None:
    project = tmp_path / "project"
    source = project / "src"
    source.mkdir(parents=True)
    target = tmp_path / "target.py"
    target.write_text("VALUE = 1\n", encoding="utf-8")
    (source / "linked.py").symlink_to(target)

    with pytest.raises(SnapshotError, match="must be a regular file"):
        fingerprint_project_source(project)


def test_a_path_object_is_accepted_wherever_a_relative_path_is(tmp_path: Path) -> None:
    source, _, snapshot = _prepared(tmp_path)

    assert snapshot.source_file(Path("region.parquet")).relative_path == "region.parquet"
    assert source_path_for(snapshot, source, Path("region.parquet")).is_file()


def test_a_non_canonical_relative_path_is_rejected() -> None:
    with pytest.raises(
        SnapshotError, match=exactly("source relative paths must use portable POSIX spelling")
    ):
        SourceFileSnapshot("a//b.parquet", 10, "a" * 64, 3, "b" * 64, 1)


def test_a_malformed_policy_value_is_rejected(tmp_path: Path) -> None:
    _, run, _ = _prepared(tmp_path)
    _rewrite(
        run, lambda payload: payload["model_identity"]["policy"].__setitem__("tie_epsilon", "high")
    )

    with pytest.raises(SnapshotError, match="must be a real number"):
        read_snapshot(run)


def test_an_unusable_language_scope_is_rejected(tmp_path: Path) -> None:
    _, run, _ = _prepared(tmp_path)
    _rewrite(run, lambda payload: payload["model_identity"].__setitem__("language_scope", []))

    with pytest.raises(SnapshotError, match="invalid snapshot model identity"):
        read_snapshot(run)


def test_an_unresolvable_source_file_is_reported(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    source = tmp_path / "source"
    _write_source(source / "region.parquet")
    original = Path.resolve

    def _explode(self: Path, strict: bool = False) -> Path:
        if strict and self.name == "region.parquet":
            raise OSError("simulated resolution failure")
        return original(self, strict=strict)

    monkeypatch.setattr(Path, "resolve", _explode)
    with pytest.raises(SnapshotError, match="cannot resolve source file"):
        inspect_source_file(source, source / "region.parquet")


def test_an_existing_empty_run_directory_is_usable(tmp_path: Path) -> None:
    source = tmp_path / "source"
    _write_source(source / "region.parquet")
    run = tmp_path / "run"
    run.mkdir()

    snapshot = _prepare(source, run)

    assert (run / "snapshot.json").is_file()
    assert read_snapshot(run) == snapshot


def test_non_parquet_files_in_the_source_tree_are_ignored(tmp_path: Path) -> None:
    source = tmp_path / "source"
    _write_source(source / "region.parquet")
    (source / "notes.txt").write_text("ignored", encoding="utf-8")
    (source / "nested").mkdir()

    snapshot = _prepare(source, tmp_path / "run")

    assert [item.relative_path for item in snapshot.source_files] == ["region.parquet"]


def test_legacy_lingua_payload_without_cascade_fields_remains_readable(
    tmp_path: Path,
) -> None:
    _, run, snapshot = _prepared(tmp_path)
    payload = json.loads((run / "snapshot.json").read_text(encoding="utf-8"))
    for key in (
        "detector_name",
        "model_repository",
        "model_filename",
        "model_revision",
        "runtime_library_name",
        "runtime_library_version",
    ):
        payload["model_identity"].pop(key)
    legacy_payload = dict(payload)
    legacy_payload["model_identity"] = {
        key: value
        for key, value in payload["model_identity"].items()
        if key in snapshot_module._LEGACY_MODEL_FIELDS
    }
    legacy_payload.pop("snapshot_id")
    payload["snapshot_id"] = snapshot_module._sha256_json(legacy_payload)
    (run / "snapshot.json").write_text(json.dumps(payload), encoding="utf-8")

    assert read_snapshot(run).model_identity == language_model_identity(LanguagePolicy())


def test_cascade_snapshot_requires_a_verified_binary_hash(tmp_path: Path) -> None:
    source = tmp_path / "source"
    _write_source(source / "region.parquet")
    run = tmp_path / "run"
    identity = cascade_model_identity(LanguagePolicy())
    prepare_snapshot(
        source,
        run,
        code_fingerprint="a" * 64,
        lock_fingerprint="b" * 64,
        model_identity=identity,
    )

    _rewrite(
        run, lambda payload: payload["model_identity"].__setitem__("binary_artifact_hash", None)
    )
    with pytest.raises(SnapshotError, match=exactly("pinned binary artifact hash is required")):
        read_snapshot(run)

    _rewrite(
        run,
        lambda payload: payload["model_identity"].__setitem__("binary_artifact_hash", "0" * 64),
    )
    with pytest.raises(
        SnapshotError,
        match=exactly("model identity field does not match derived value: binary_artifact_hash"),
    ):
        read_snapshot(run)

    _rewrite(
        run,
        lambda payload: payload["model_identity"].__setitem__("binary_artifact_hash", True),
    )
    with pytest.raises(
        SnapshotError,
        match=exactly("snapshot field binary_artifact_hash must be a string or null"),
    ):
        read_snapshot(run)


def test_an_optional_model_identity_field_names_itself_when_it_is_not_text(
    tmp_path: Path,
) -> None:
    """The operator needs the field name: six optional fields share this refusal."""
    _, run, _ = _prepared(tmp_path)
    _rewrite(run, lambda payload: payload["model_identity"].__setitem__("model_repository", 7))

    with pytest.raises(
        SnapshotError, match=exactly("snapshot field model_repository must be a string or null")
    ):
        read_snapshot(run)


def test_an_absent_optional_identity_field_does_not_stop_the_remaining_checks(
    tmp_path: Path,
) -> None:
    """An absent optional field must skip only itself, not every later field."""

    def drop_optional_and_corrupt_later(payload: dict[str, Any]) -> None:
        payload["model_identity"].pop("model_repository")
        payload["model_identity"]["config_fingerprint"] = "c" * 64

    _, run, _ = _prepared(tmp_path)
    _rewrite(run, drop_optional_and_corrupt_later)

    with pytest.raises(
        SnapshotError,
        match=exactly("model identity field does not match derived value: config_fingerprint"),
    ):
        read_snapshot(run)


_SPLITTER_FIELDS = ("splitter_name", "splitter_revision", "splitter_languages_fingerprint")


def test_the_snapshot_names_the_splitter_that_produced_the_run(tmp_path: Path) -> None:
    """The splitter binds through config_fingerprint, but nobody can read a hash.

    A person opening snapshot.json has to be able to say which sentence
    splitter made these sentences, without recomputing a fingerprint.
    """
    source, run, manifest = _prepared(tmp_path)

    recorded = json.loads((run / "snapshot.json").read_text(encoding="utf-8"))["model_identity"]

    assert recorded["splitter_name"] == manifest.model_identity.splitter_name
    assert recorded["splitter_revision"] == manifest.model_identity.splitter_revision
    assert (
        recorded["splitter_languages_fingerprint"]
        == manifest.model_identity.splitter_languages_fingerprint
    )


def test_the_recorded_splitter_is_the_pinned_sat_artifact(tmp_path: Path) -> None:
    """Naming the wrong splitter would be worse than naming none at all."""
    _, run, _ = _prepared(tmp_path)

    recorded = json.loads((run / "snapshot.json").read_text(encoding="utf-8"))["model_identity"]

    assert recorded["splitter_name"] == "sat-3l-sm"
    assert recorded["splitter_revision"] == "137da054051ad9f1eac42025f758db4ac9f22535"


def test_a_snapshot_carrying_the_splitter_still_round_trips(tmp_path: Path) -> None:
    _, run, manifest = _prepared(tmp_path)

    assert read_snapshot(run) == manifest


@pytest.mark.parametrize("field", _SPLITTER_FIELDS)
def test_a_recorded_splitter_field_that_disagrees_is_refused(tmp_path: Path, field: str) -> None:
    """A run claiming one splitter and computed with another is not publishable."""
    _, run, _ = _prepared(tmp_path)
    _rewrite(run, lambda payload: payload["model_identity"].update({field: "tampered"}))

    with pytest.raises(
        SnapshotError,
        match=exactly(f"model identity field does not match derived value: {field}"),
    ):
        read_snapshot(run)


@pytest.mark.parametrize("field", _SPLITTER_FIELDS)
def test_a_model_payload_without_the_splitter_keys_is_read_not_refused(
    tmp_path: Path, field: str
) -> None:
    """A missing key has nothing to disagree with, so it must not be a refusal.

    This is about parsing the identity, not about the snapshot id. A snapshot
    frozen before these keys existed had its id hashed over a payload without
    them, so the id itself no longer verifies; see the test below.
    """
    _, run, _ = _prepared(tmp_path)
    _rewrite(run, lambda payload: payload["model_identity"].pop(field))

    assert read_snapshot(run).model_identity.splitter_name == "sat-3l-sm"


def test_a_snapshot_frozen_before_the_splitter_was_recorded_must_be_re_prepared(
    tmp_path: Path,
) -> None:
    """Recording the splitter changes the id, and that has to be visible.

    Every id is a hash over the recorded payload, so widening the payload
    retires the ids that came before it. A stale run directory must be refused
    loudly and re-prepared, never silently accepted against new code.
    """
    _, run, manifest = _prepared(tmp_path)

    def to_the_old_shape(payload: dict[str, Any]) -> None:
        for name in _SPLITTER_FIELDS:
            payload["model_identity"].pop(name)
        identity = {key: value for key, value in payload.items() if key != "snapshot_id"}
        payload["snapshot_id"] = snapshot_module._sha256_json(identity)

    _rewrite(run, to_the_old_shape)

    with pytest.raises(
        SnapshotError, match=exactly("snapshot id does not match canonical content")
    ):
        read_snapshot(run)

    assert json.loads((run / "snapshot.json").read_text(encoding="utf-8"))["snapshot_id"] != (
        manifest.snapshot_id
    )


def test_the_legacy_payload_never_gained_the_splitter_fields() -> None:
    """Legacy ids were hashed over exactly these names; adding one breaks them."""
    assert snapshot_module._LEGACY_MODEL_FIELDS == (
        "library_name",
        "library_version",
        "language_scope",
        "policy",
        "policy_fingerprint",
        "config_fingerprint",
        "binary_artifact_hash",
    )


# Portable snapshot bytes, strict persisted fields, and exact diagnostics.


def _canonical(payload: object) -> bytes:
    return json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()


def test_source_metadata_accepts_zero_counts_without_coercion() -> None:
    item = snapshot_module.SourceFileSnapshot("empty.parquet", 0, "a" * 64, 3, "b" * 64, 0)
    assert item.to_payload() == {
        "relative_path": "empty.parquet",
        "size_bytes": 0,
        "sha256": "a" * 64,
        "schema_version": 3,
        "schema_fingerprint": "b" * 64,
        "row_count": 0,
    }


def test_non_utf8_metadata_key_is_refused_before_writing_snapshot(tmp_path: Path) -> None:
    source = tmp_path / "source"
    source.mkdir()
    run = tmp_path / "run"
    schema = SCHEMA.with_metadata({b"\xff": b"opaque-value"})
    pq.write_table(pa.Table.from_batches([], schema=schema), source / "region.parquet")

    with pytest.raises(snapshot_module.SnapshotError, match="^source metadata keys must be UTF-8$"):
        snapshot_module.prepare_snapshot(
            source, run, code_fingerprint="a" * 64, lock_fingerprint="b" * 64
        )
    assert not (run / "snapshot.json").exists()


@pytest.fixture
def prepared_snapshot(tmp_path: Path) -> tuple[Path, Path, SnapshotManifest]:
    source, run = tmp_path / "source", tmp_path / "run"
    source.mkdir()
    schema = SCHEMA.with_metadata({b"origin": b"synthetic", "é".encode(): b"\x00\xff"})
    pq.write_table(pa.Table.from_batches([], schema=schema), source / "région.parquet")
    snapshot = snapshot_module.prepare_snapshot(
        source,
        run,
        code_fingerprint="a" * 64,
        lock_fingerprint="b" * 64,
        policy=LanguagePolicy(min_alphabetic_chars=7, tie_epsilon=0.001),
        language_scope=("fra", "eng"),
    )
    return source, run, snapshot


def test_prepared_bytes_bind_exact_schema_metadata_policy_and_source(
    prepared_snapshot: tuple,
) -> None:
    source, run, snapshot = prepared_snapshot
    path = next(source.iterdir())
    schema = pq.ParquetFile(path).schema_arrow
    schema_payload = {
        "fields": [{"name": f.name, "type": str(f.type), "nullable": f.nullable} for f in schema],
        "metadata": {key.decode(): value.hex() for key, value in schema.metadata.items()},
    }
    expected_relative_path = next(source.iterdir()).name
    assert snapshot.source_files[0].to_payload() == {
        "relative_path": expected_relative_path,
        "size_bytes": len(path.read_bytes()),
        "sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
        "schema_version": 3,
        "schema_fingerprint": hashlib.sha256(_canonical(schema_payload)).hexdigest(),
        "row_count": 0,
    }
    expected_identity = language_model_identity(
        LanguagePolicy(min_alphabetic_chars=7, tie_epsilon=0.001),
        language_scope=("eng", "fra"),
    )
    assert snapshot.model_identity == expected_identity
    payload = snapshot.to_payload()
    identity_payload = {key: value for key, value in payload.items() if key != "snapshot_id"}
    assert snapshot.snapshot_id == hashlib.sha256(_canonical(identity_payload)).hexdigest()
    assert (run / "snapshot.json").read_bytes() == _canonical(payload) + b"\n"
    assert snapshot_module.read_snapshot(run) == snapshot
    assert (
        snapshot_module.verify_source_file(snapshot, source, Path(expected_relative_path))
        == snapshot.source_files[0]
    )
    assert snapshot_module.verify_all_source_files(snapshot, source) == snapshot.source_files


@pytest.mark.parametrize(
    ("trail", "value", "message"),
    [
        (
            ("model_identity", "policy", "min_alphabetic_chars"),
            True,
            "snapshot field min_alphabetic_chars must be an integer",
        ),
        (
            ("model_identity", "policy", "tie_epsilon"),
            "0.25",
            "snapshot field tie_epsilon must be a real number",
        ),
        (
            ("model_identity", "policy", "tie_epsilon"),
            None,
            "snapshot field tie_epsilon must be a real number",
        ),
        (
            ("model_identity", "policy", "tie_epsilon"),
            2,
            "invalid snapshot policy: tie_epsilon must be finite and between 0 and 1.0",
        ),
        (
            ("model_identity", "language_scope"),
            [],
            "invalid snapshot model identity: language_scope must contain non-empty strings",
        ),
        (("model_identity", "library_name"), None, "snapshot field library_name must be a string"),
        (
            ("model_identity", "library_version"),
            "other",
            "model identity field does not match derived value: library_version",
        ),
        (
            ("model_identity", "binary_artifact_hash"),
            "a" * 64,
            "binary artifact hash must remain unset until independently verified",
        ),
        (
            ("source_files", 0, "schema_fingerprint"),
            7,
            "source file field schema_fingerprint must be a string",
        ),
        (("source_files", 0, "row_count"), True, "source file field row_count must be an integer"),
        (
            ("source_files", 0, "relative_path"),
            "a//b.parquet",
            "source relative paths must use portable POSIX spelling",
        ),
        (("source_files", 0, "size_bytes"), -1, "source size_bytes must be a non-negative integer"),
        (("lock_fingerprint",), None, "snapshot field lock_fingerprint must be a string"),
    ],
)
def test_read_snapshot_reports_exact_nested_field_errors(
    prepared_snapshot: tuple, trail: tuple[str | int, ...], value: object, message: str
) -> None:
    _, run, snapshot = prepared_snapshot
    payload = snapshot.to_payload()
    target: Any = payload
    for key in trail[:-1]:
        target = target[key]
    target[trail[-1]] = value
    (run / "snapshot.json").write_bytes(_canonical(payload))
    with pytest.raises(snapshot_module.SnapshotError) as caught:
        snapshot_module.read_snapshot(run)
    assert str(caught.value) == message


@pytest.mark.parametrize("mismatch", ["type", "nullable"])
def test_inspection_rejects_each_schema_field_difference_with_its_path(
    tmp_path: Path, mismatch: str
) -> None:
    schema = SCHEMA
    field = schema.field(0)
    changed = pa.field(
        field.name,
        pa.string() if field.type != pa.string() else pa.int64(),
        nullable=field.nullable,
    )
    if mismatch == "nullable":
        changed = pa.field(field.name, field.type, nullable=not field.nullable)
    schema = schema.set(0, changed)
    path = tmp_path / "invalid.parquet"
    pq.write_table(pa.Table.from_batches([], schema=schema), path)
    with pytest.raises(snapshot_module.SnapshotError) as caught:
        snapshot_module.inspect_source_file(tmp_path, path)
    assert (
        str(caught.value)
        == f"source Parquet schema field mismatch for {field.name}: {path.resolve()}"
    )


@pytest.mark.parametrize(
    "operation", ["prepare", "lookup", "verify_all", "project_source", "lockfile"]
)
def test_directory_diagnostics_preserve_the_callers_label(
    prepared_snapshot: tuple, tmp_path: Path, operation: str
) -> None:
    source, _, snapshot = prepared_snapshot
    source_name = next(source.iterdir()).name
    wrong = tmp_path / "not-a-directory"
    wrong.write_bytes(b"file")
    actions = {
        "prepare": lambda: snapshot_module.prepare_snapshot(
            wrong, tmp_path / "new-run", code_fingerprint="a" * 64, lock_fingerprint="b" * 64
        ),
        "lookup": lambda: snapshot_module.source_path_for(snapshot, wrong, source_name),
        "verify_all": lambda: snapshot_module.verify_all_source_files(snapshot, wrong),
        "project_source": lambda: snapshot_module.fingerprint_project_source(wrong),
        "lockfile": lambda: snapshot_module.fingerprint_lockfile(wrong),
    }
    with pytest.raises(snapshot_module.SnapshotError) as caught:
        actions[operation]()
    label = "project root" if operation in {"project_source", "lockfile"} else "source directory"
    assert str(caught.value) == f"{label} is not a directory: {wrong}"


@pytest.mark.parametrize("field", ["code_fingerprint", "lock_fingerprint"])
def test_prepare_fingerprint_diagnostics_name_the_argument(
    prepared_snapshot: tuple, field: str
) -> None:
    source, run, _ = prepared_snapshot
    kwargs = {"code_fingerprint": "a" * 64, "lock_fingerprint": "b" * 64}
    kwargs[field] = "INVALID"
    with pytest.raises(snapshot_module.SnapshotError) as caught:
        snapshot_module.prepare_snapshot(source, run, **kwargs)
    assert (
        str(caught.value)
        == f"{field.replace('_', ' ')} must be a lowercase SHA-256 hex fingerprint"
    )


def test_lookup_rejects_symlink_even_when_target_stays_inside_source(
    prepared_snapshot: tuple,
) -> None:
    source, _, snapshot = prepared_snapshot
    path = next(source.iterdir())
    relative_path = path.name
    path.rename(source / "moved.parquet")
    path.symlink_to(source / "moved.parquet")
    with pytest.raises(snapshot_module.SnapshotError) as caught:
        snapshot_module.source_path_for(snapshot, source, Path(relative_path))
    assert str(caught.value) == f"source path escapes or uses a symlink: {relative_path}"


def test_project_fingerprint_is_exact_and_uses_posix_relative_path_order(tmp_path: Path) -> None:
    project = tmp_path / "project"
    files = {"src/z.py": b"z", "src/a/x.py": b"nested", "src/a.py": "é".encode()}
    for name, content in files.items():
        path = project / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(content)
    entries = [
        {
            "relative_path": name,
            "size_bytes": len(files[name]),
            "sha256": hashlib.sha256(files[name]).hexdigest(),
        }
        for name in sorted(files)
    ]
    assert (
        snapshot_module.fingerprint_project_source(project)
        == hashlib.sha256(_canonical({"files": entries})).hexdigest()
    )


def test_read_snapshot_uses_the_selected_manifest_and_explicit_utf8(
    prepared_snapshot: tuple, monkeypatch: pytest.MonkeyPatch
) -> None:
    _, run, snapshot = prepared_snapshot
    original = Path.read_text
    calls: list[tuple[Path, str | None]] = []

    def read_text(path: Path, encoding: str | None = None, errors: str | None = None) -> str:
        calls.append((path, encoding))
        return original(path, encoding=encoding, errors=errors)

    monkeypatch.setattr(Path, "read_text", read_text)
    assert snapshot_module.read_snapshot(run) == snapshot
    assert len(calls) == 1
    assert calls[0][0] == run / "snapshot.json"
    assert calls[0][1] is not None
    assert calls[0][1].lower().replace("-", "") == "utf8"


@pytest.mark.parametrize("case", ["inside", "contains", "occupied", "identity", "extra", "model"])
def test_prepare_and_verification_diagnostics_describe_exact_conflicts(
    prepared_snapshot: tuple, tmp_path: Path, case: str
) -> None:
    source, run, snapshot = prepared_snapshot
    target = tmp_path / "fresh-run"
    kwargs: dict[str, Any] = {"code_fingerprint": "a" * 64, "lock_fingerprint": "b" * 64}
    messages = {
        "inside": "run output must be outside the immutable source directory",
        "contains": "run output must not contain the immutable source directory",
        "occupied": "cannot initialize snapshot in a non-empty run directory",
        "identity": "immutable snapshot identity differs from requested inputs",
        "extra": "source directory files do not match immutable snapshot",
        "model": "model_identity must be a LanguageModelIdentity",
    }
    if case == "inside":
        target = source / "run"
    elif case == "contains":
        target = source.parent
    elif case == "occupied":
        target.mkdir()
        (target / "keep.txt").write_text("occupied")
    elif case == "identity":
        target = run
        kwargs["code_fingerprint"] = "c" * 64
    elif case == "model":
        kwargs["model_identity"] = object()
    else:
        (source / "extra.parquet").write_bytes(next(source.iterdir()).read_bytes())
    with pytest.raises(TypeError if case == "model" else snapshot_module.SnapshotError) as caught:
        if case == "extra":
            snapshot_module.verify_all_source_files(snapshot, source)
        else:
            snapshot_module.prepare_snapshot(source, target, **kwargs)
    assert str(caught.value) == messages[case]


@pytest.mark.parametrize("duplicate", [False, True])
def test_read_manifest_rejects_source_order_and_duplicates_with_exact_diagnostics(
    prepared_snapshot: tuple, duplicate: bool
) -> None:
    _, run, snapshot = prepared_snapshot
    payload = snapshot.to_payload()
    first = snapshot.source_files[0].to_payload()
    payload["source_files"] = [
        first,
        first if duplicate else {**first, "relative_path": "a.parquet"},
    ]
    (run / "snapshot.json").write_bytes(_canonical(payload))
    with pytest.raises(snapshot_module.SnapshotError) as caught:
        snapshot_module.read_snapshot(run)
    assert str(caught.value) == (
        "source files must not contain duplicate relative paths"
        if duplicate
        else "source files must be sorted by relative path"
    )


def test_prepare_orders_nested_sources_by_portable_path_spelling(prepared_snapshot: tuple) -> None:
    source, run, _ = prepared_snapshot
    original_name = next(source.iterdir()).name
    data = (source / original_name).read_bytes()
    (source / "a").mkdir()
    (source / "a" / "nested.parquet").write_bytes(data)
    (source / "a.parquet").write_bytes(data)
    snapshot = snapshot_module.prepare_snapshot(
        source, run.parent / "ordered-run", code_fingerprint="a" * 64, lock_fingerprint="b" * 64
    )
    assert [item.relative_path for item in snapshot.source_files] == [
        "a.parquet",
        "a/nested.parquet",
        original_name,
    ]


def test_lockfile_fingerprint_is_the_sha256_of_the_exact_lockfile_bytes(tmp_path: Path) -> None:
    (tmp_path / "uv.lock").write_bytes(b"synthetic lock\r\n")

    assert (
        snapshot_module.fingerprint_lockfile(tmp_path)
        == hashlib.sha256(b"synthetic lock\r\n").hexdigest()
    )
