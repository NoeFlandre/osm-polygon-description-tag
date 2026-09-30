"""Immutable snapshot identity, strict payloads, and path containment."""

import hashlib
import json
from pathlib import Path
from typing import Any

import pyarrow as pa
import pyarrow.parquet as pq
import pytest

from osm_polygon_description_tag.dataset.languages import snapshot as snapshot_module
from osm_polygon_description_tag.dataset.languages.models import (
    LanguagePolicy,
    cascade_model_identity,
    language_model_identity,
)
from osm_polygon_description_tag.dataset.languages.snapshot import (
    SnapshotError,
    SnapshotManifest,
    SourceFileSnapshot,
    fingerprint_project_source,
    inspect_source_file,
    prepare_snapshot,
    read_snapshot,
    source_path_for,
)
from osm_polygon_description_tag.dataset.schema import SCHEMA
from tests.helpers.language_setup import LanguageRunSetup
from tests.helpers.language_setup import rewrite_snapshot as _rewrite
from tests.helpers.messages import exactly


def test_project_source_fingerprint_rejects_symlinked_files(tmp_path: Path) -> None:
    project = tmp_path / "project"
    source = project / "src"
    source.mkdir(parents=True)
    target = tmp_path / "target.py"
    target.write_text("VALUE = 1\n", encoding="utf-8")
    (source / "linked.py").symlink_to(target)

    with pytest.raises(SnapshotError, match="must be a regular file"):
        fingerprint_project_source(project)


def test_a_path_object_is_accepted_wherever_a_relative_path_is(
    tmp_path: Path, language_run_setup: LanguageRunSetup
) -> None:
    source, _, snapshot = language_run_setup.prepared_snapshot(tmp_path)

    assert snapshot.source_file(Path("region.parquet")).relative_path == "region.parquet"
    assert source_path_for(snapshot, source, Path("region.parquet")).is_file()


def test_a_non_canonical_relative_path_is_rejected() -> None:
    with pytest.raises(
        SnapshotError, match=exactly("source relative paths must use portable POSIX spelling")
    ):
        SourceFileSnapshot("a//b.parquet", 10, "a" * 64, 3, "b" * 64, 1)


def test_a_malformed_policy_value_is_rejected(
    tmp_path: Path, language_run_setup: LanguageRunSetup
) -> None:
    _, run, _ = language_run_setup.prepared_snapshot(tmp_path)
    _rewrite(
        run, lambda payload: payload["model_identity"]["policy"].__setitem__("tie_epsilon", "high")
    )

    with pytest.raises(SnapshotError, match="must be a real number"):
        read_snapshot(run)


def test_an_unusable_language_scope_is_rejected(
    tmp_path: Path, language_run_setup: LanguageRunSetup
) -> None:
    _, run, _ = language_run_setup.prepared_snapshot(tmp_path)
    _rewrite(run, lambda payload: payload["model_identity"].__setitem__("language_scope", []))

    with pytest.raises(SnapshotError, match="invalid snapshot model identity"):
        read_snapshot(run)


def test_an_unresolvable_source_file_is_reported(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    language_run_setup: LanguageRunSetup,
) -> None:
    source = tmp_path / "source"
    language_run_setup.write_snapshot_source(source / "region.parquet")
    original = Path.resolve

    def _explode(self: Path, strict: bool = False) -> Path:
        if strict and self.name == "region.parquet":
            raise OSError("simulated resolution failure")
        return original(self, strict=strict)

    monkeypatch.setattr(Path, "resolve", _explode)
    with pytest.raises(SnapshotError, match="cannot resolve source file"):
        inspect_source_file(source, source / "region.parquet")


def test_an_existing_empty_run_directory_is_usable(
    tmp_path: Path, language_run_setup: LanguageRunSetup
) -> None:
    source = tmp_path / "source"
    language_run_setup.write_snapshot_source(source / "region.parquet")
    run = tmp_path / "run"
    run.mkdir()

    snapshot = language_run_setup.prepare_snapshot(source, run)

    assert (run / "snapshot.json").is_file()
    assert read_snapshot(run) == snapshot


def test_non_parquet_files_in_the_source_tree_are_ignored(
    tmp_path: Path, language_run_setup: LanguageRunSetup
) -> None:
    source = tmp_path / "source"
    language_run_setup.write_snapshot_source(source / "region.parquet")
    (source / "notes.txt").write_text("ignored", encoding="utf-8")
    (source / "nested").mkdir()

    snapshot = language_run_setup.prepare_snapshot(source, tmp_path / "run")

    assert [item.relative_path for item in snapshot.source_files] == ["region.parquet"]


def test_legacy_lingua_payload_without_cascade_fields_remains_readable(
    tmp_path: Path,
    language_run_setup: LanguageRunSetup,
) -> None:
    _, run, snapshot = language_run_setup.prepared_snapshot(tmp_path)
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


def test_cascade_snapshot_requires_a_verified_binary_hash(
    tmp_path: Path, language_run_setup: LanguageRunSetup
) -> None:
    source = tmp_path / "source"
    language_run_setup.write_snapshot_source(source / "region.parquet")
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
    language_run_setup: LanguageRunSetup,
) -> None:
    """The operator needs the field name: six optional fields share this refusal."""
    _, run, _ = language_run_setup.prepared_snapshot(tmp_path)
    _rewrite(run, lambda payload: payload["model_identity"].__setitem__("model_repository", 7))

    with pytest.raises(
        SnapshotError, match=exactly("snapshot field model_repository must be a string or null")
    ):
        read_snapshot(run)


def test_an_absent_optional_identity_field_does_not_stop_the_remaining_checks(
    tmp_path: Path,
    language_run_setup: LanguageRunSetup,
) -> None:
    """An absent optional field must skip only itself, not every later field."""

    def drop_optional_and_corrupt_later(payload: dict[str, Any]) -> None:
        payload["model_identity"].pop("model_repository")
        payload["model_identity"]["config_fingerprint"] = "c" * 64

    _, run, _ = language_run_setup.prepared_snapshot(tmp_path)
    _rewrite(run, drop_optional_and_corrupt_later)

    with pytest.raises(
        SnapshotError,
        match=exactly("model identity field does not match derived value: config_fingerprint"),
    ):
        read_snapshot(run)


_SPLITTER_FIELDS = ("splitter_name", "splitter_revision", "splitter_languages_fingerprint")


def test_the_snapshot_names_the_splitter_that_produced_the_run(
    tmp_path: Path, language_run_setup: LanguageRunSetup
) -> None:
    """The splitter binds through config_fingerprint, but nobody can read a hash.

    A person opening snapshot.json has to be able to say which sentence
    splitter made these sentences, without recomputing a fingerprint.
    """
    source, run, manifest = language_run_setup.prepared_snapshot(tmp_path)

    recorded = json.loads((run / "snapshot.json").read_text(encoding="utf-8"))["model_identity"]

    assert recorded["splitter_name"] == manifest.model_identity.splitter_name
    assert recorded["splitter_revision"] == manifest.model_identity.splitter_revision
    assert (
        recorded["splitter_languages_fingerprint"]
        == manifest.model_identity.splitter_languages_fingerprint
    )


def test_the_recorded_splitter_is_the_pinned_sat_artifact(
    tmp_path: Path, language_run_setup: LanguageRunSetup
) -> None:
    """Naming the wrong splitter would be worse than naming none at all."""
    _, run, _ = language_run_setup.prepared_snapshot(tmp_path)

    recorded = json.loads((run / "snapshot.json").read_text(encoding="utf-8"))["model_identity"]

    assert recorded["splitter_name"] == "sat-3l-sm"
    assert recorded["splitter_revision"] == "137da054051ad9f1eac42025f758db4ac9f22535"


def test_a_snapshot_carrying_the_splitter_still_round_trips(
    tmp_path: Path, language_run_setup: LanguageRunSetup
) -> None:
    _, run, manifest = language_run_setup.prepared_snapshot(tmp_path)

    assert read_snapshot(run) == manifest


@pytest.mark.parametrize("field", _SPLITTER_FIELDS)
def test_a_recorded_splitter_field_that_disagrees_is_refused(
    tmp_path: Path, field: str, language_run_setup: LanguageRunSetup
) -> None:
    """A run claiming one splitter and computed with another is not publishable."""
    _, run, _ = language_run_setup.prepared_snapshot(tmp_path)
    _rewrite(run, lambda payload: payload["model_identity"].update({field: "tampered"}))

    with pytest.raises(
        SnapshotError,
        match=exactly(f"model identity field does not match derived value: {field}"),
    ):
        read_snapshot(run)


@pytest.mark.parametrize("field", _SPLITTER_FIELDS)
def test_a_model_payload_without_the_splitter_keys_is_read_not_refused(
    tmp_path: Path,
    field: str,
    language_run_setup: LanguageRunSetup,
) -> None:
    """A missing key has nothing to disagree with, so it must not be a refusal.

    This is about parsing the identity, not about the snapshot id. A snapshot
    frozen before these keys existed had its id hashed over a payload without
    them, so the id itself no longer verifies; see the test below.
    """
    _, run, _ = language_run_setup.prepared_snapshot(tmp_path)
    _rewrite(run, lambda payload: payload["model_identity"].pop(field))

    assert read_snapshot(run).model_identity.splitter_name == "sat-3l-sm"


def test_a_snapshot_frozen_before_the_splitter_was_recorded_must_be_re_prepared(
    tmp_path: Path,
    language_run_setup: LanguageRunSetup,
) -> None:
    """Recording the splitter changes the id, and that has to be visible.

    Every id is a hash over the recorded payload, so widening the payload
    retires the ids that came before it. A stale run directory must be refused
    loudly and re-prepared, never silently accepted against new code.
    """
    _, run, manifest = language_run_setup.prepared_snapshot(tmp_path)

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
