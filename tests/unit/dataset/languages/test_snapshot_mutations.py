"""Portable snapshot bytes, strict persisted fields, and boundary forwarding."""

import hashlib
import json
from pathlib import Path
from typing import Any

import pyarrow as pa
import pyarrow.parquet as pq
import pytest

from osm_polygon_description_tag.dataset.languages import snapshot as module
from osm_polygon_description_tag.dataset.languages.models import (
    LanguagePolicy,
    language_model_identity,
)
from osm_polygon_description_tag.dataset.schema import SCHEMA


def _canonical(payload: object) -> bytes:
    return json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()


def test_source_metadata_accepts_zero_counts_without_coercion() -> None:
    item = module.SourceFileSnapshot("empty.parquet", 0, "a" * 64, 3, "b" * 64, 0)
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

    with pytest.raises(module.SnapshotError, match="^source metadata keys must be UTF-8$"):
        module.prepare_snapshot(source, run, code_fingerprint="a" * 64, lock_fingerprint="b" * 64)
    assert not (run / "snapshot.json").exists()


@pytest.fixture
def prepared(tmp_path: Path) -> tuple[Path, Path, module.SnapshotManifest]:
    source, run = tmp_path / "source", tmp_path / "run"
    source.mkdir()
    schema = SCHEMA.with_metadata({b"origin": b"synthetic", "é".encode(): b"\x00\xff"})
    pq.write_table(pa.Table.from_batches([], schema=schema), source / "région.parquet")
    snapshot = module.prepare_snapshot(
        source,
        run,
        code_fingerprint="a" * 64,
        lock_fingerprint="b" * 64,
        policy=LanguagePolicy(min_alphabetic_chars=7, tie_epsilon=0.001),
        language_scope=("fra", "eng"),
    )
    return source, run, snapshot


def test_prepared_bytes_bind_exact_schema_metadata_policy_and_source(prepared: tuple) -> None:
    source, run, snapshot = prepared
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
    assert module.read_snapshot(run) == snapshot
    assert (
        module.verify_source_file(snapshot, source, Path(expected_relative_path))
        == snapshot.source_files[0]
    )
    assert module.verify_all_source_files(snapshot, source) == snapshot.source_files


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
    prepared: tuple, trail: tuple[str | int, ...], value: object, message: str
) -> None:
    _, run, snapshot = prepared
    payload = snapshot.to_payload()
    target: Any = payload
    for key in trail[:-1]:
        target = target[key]
    target[trail[-1]] = value
    (run / "snapshot.json").write_bytes(_canonical(payload))
    with pytest.raises(module.SnapshotError) as caught:
        module.read_snapshot(run)
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
    with pytest.raises(module.SnapshotError) as caught:
        module.inspect_source_file(tmp_path, path)
    assert (
        str(caught.value)
        == f"source Parquet schema field mismatch for {field.name}: {path.resolve()}"
    )


@pytest.mark.parametrize(
    "operation", ["prepare", "lookup", "verify_all", "project_source", "lockfile"]
)
def test_directory_diagnostics_preserve_the_callers_label(
    prepared: tuple, tmp_path: Path, operation: str
) -> None:
    source, _, snapshot = prepared
    source_name = next(source.iterdir()).name
    wrong = tmp_path / "not-a-directory"
    wrong.write_bytes(b"file")
    actions = {
        "prepare": lambda: module.prepare_snapshot(
            wrong, tmp_path / "new-run", code_fingerprint="a" * 64, lock_fingerprint="b" * 64
        ),
        "lookup": lambda: module.source_path_for(snapshot, wrong, source_name),
        "verify_all": lambda: module.verify_all_source_files(snapshot, wrong),
        "project_source": lambda: module.fingerprint_project_source(wrong),
        "lockfile": lambda: module.fingerprint_lockfile(wrong),
    }
    with pytest.raises(module.SnapshotError) as caught:
        actions[operation]()
    label = "project root" if operation in {"project_source", "lockfile"} else "source directory"
    assert str(caught.value) == f"{label} is not a directory: {wrong}"


@pytest.mark.parametrize("field", ["code_fingerprint", "lock_fingerprint"])
def test_prepare_fingerprint_diagnostics_name_the_argument(prepared: tuple, field: str) -> None:
    source, run, _ = prepared
    kwargs = {"code_fingerprint": "a" * 64, "lock_fingerprint": "b" * 64}
    kwargs[field] = "INVALID"
    with pytest.raises(module.SnapshotError) as caught:
        module.prepare_snapshot(source, run, **kwargs)
    assert (
        str(caught.value)
        == f"{field.replace('_', ' ')} must be a lowercase SHA-256 hex fingerprint"
    )


def test_lookup_rejects_symlink_even_when_target_stays_inside_source(prepared: tuple) -> None:
    source, _, snapshot = prepared
    path = next(source.iterdir())
    relative_path = path.name
    path.rename(source / "moved.parquet")
    path.symlink_to(source / "moved.parquet")
    with pytest.raises(module.SnapshotError) as caught:
        module.source_path_for(snapshot, source, Path(relative_path))
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
        module.fingerprint_project_source(project)
        == hashlib.sha256(_canonical({"files": entries})).hexdigest()
    )


def test_project_verification_forwards_exact_project_and_checks_both_identities(
    prepared: tuple, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _, _, snapshot = prepared
    project = tmp_path / "selected-project"
    calls: list[tuple[str, Path]] = []

    def source_digest(root: Path) -> str:
        calls.append(("source", root))
        return snapshot.code_fingerprint

    def lock_digest(root: Path) -> str:
        calls.append(("lock", root))
        return snapshot.lock_fingerprint

    monkeypatch.setattr(module, "fingerprint_project_source", source_digest)
    monkeypatch.setattr(module, "fingerprint_lockfile", lock_digest)
    assert module.verify_project_identity(snapshot, project) is None
    assert calls == [("source", project), ("lock", project)]


def test_read_snapshot_uses_the_selected_manifest_and_explicit_utf8(
    prepared: tuple, monkeypatch: pytest.MonkeyPatch
) -> None:
    _, run, snapshot = prepared
    original = Path.read_text
    calls: list[tuple[Path, str | None]] = []

    def read_text(path: Path, encoding: str | None = None, errors: str | None = None) -> str:
        calls.append((path, encoding))
        return original(path, encoding=encoding, errors=errors)

    monkeypatch.setattr(Path, "read_text", read_text)
    assert module.read_snapshot(run) == snapshot
    assert len(calls) == 1
    assert calls[0][0] == run / "snapshot.json"
    assert calls[0][1] is not None
    assert calls[0][1].lower().replace("-", "") == "utf8"


@pytest.mark.parametrize("case", ["inside", "contains", "occupied", "identity", "extra", "model"])
def test_prepare_and_verification_diagnostics_describe_exact_conflicts(
    prepared: tuple, tmp_path: Path, case: str
) -> None:
    source, run, snapshot = prepared
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
    with pytest.raises(TypeError if case == "model" else module.SnapshotError) as caught:
        if case == "extra":
            module.verify_all_source_files(snapshot, source)
        else:
            module.prepare_snapshot(source, target, **kwargs)
    assert str(caught.value) == messages[case]


@pytest.mark.parametrize("duplicate", [False, True])
def test_read_manifest_rejects_source_order_and_duplicates_with_exact_diagnostics(
    prepared: tuple, duplicate: bool
) -> None:
    _, run, snapshot = prepared
    payload = snapshot.to_payload()
    first = snapshot.source_files[0].to_payload()
    payload["source_files"] = [
        first,
        first if duplicate else {**first, "relative_path": "a.parquet"},
    ]
    (run / "snapshot.json").write_bytes(_canonical(payload))
    with pytest.raises(module.SnapshotError) as caught:
        module.read_snapshot(run)
    assert str(caught.value) == (
        "source files must not contain duplicate relative paths"
        if duplicate
        else "source files must be sorted by relative path"
    )


def test_lockfile_fingerprinting_forwards_exact_path_and_bytes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "uv.lock"
    path.write_bytes(b"synthetic lock\r\n")
    calls: list[Path] = []

    def digest(selected: Path) -> str:
        calls.append(selected)
        return hashlib.sha256(selected.read_bytes()).hexdigest()

    monkeypatch.setattr(module, "file_sha256", digest)
    assert (
        module.fingerprint_lockfile(tmp_path) == hashlib.sha256(b"synthetic lock\r\n").hexdigest()
    )
    assert [str(selected) for selected in calls] == [str(path)]


def test_prepare_orders_nested_sources_by_portable_path_spelling(prepared: tuple) -> None:
    source, run, _ = prepared
    original_name = next(source.iterdir()).name
    data = (source / original_name).read_bytes()
    (source / "a").mkdir()
    (source / "a" / "nested.parquet").write_bytes(data)
    (source / "a.parquet").write_bytes(data)
    snapshot = module.prepare_snapshot(
        source, run.parent / "ordered-run", code_fingerprint="a" * 64, lock_fingerprint="b" * 64
    )
    assert [item.relative_path for item in snapshot.source_files] == [
        "a.parquet",
        "a/nested.parquet",
        original_name,
    ]
