"""Direct contracts for deduplication staging, promotion, and state."""

import hashlib
from collections.abc import Mapping
from datetime import UTC
from pathlib import Path
from types import SimpleNamespace

import pytest

import osm_polygon_description_tag.dataset.deduplication as dedup_module
from osm_polygon_description_tag.dataset import canonical_rows
from osm_polygon_description_tag.dataset.deduplication import (
    _STATE_RELATIVE_PATH,
    DEDUPLICATION_POLICY_SHA256,
    DEDUPLICATION_POLICY_VERSION,
    DeduplicationError,
    DeduplicationResult,
    _canonical_relation,
    _complete_result,
    _DeduplicationContext,
    _finish_deduplication,
    _prepare_context,
    _promote_artifact,
    _promote_entry,
    _promote_staged,
    _read_manifests,
    _resume_staged,
    _skipped_result,
    _stage_changes,
    _state_payload,
    deduplicate_dataset,
    select_canonical_row,
)
from osm_polygon_description_tag.dataset.manifest import (
    file_sha256,
)
from osm_polygon_description_tag.dataset.storage import write_geoparquet
from tests.helpers.dataset import two_deduplication_records as _two_records


def test_read_manifests_uses_the_lowercase_manifest_directory_name(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    parquet = tmp_path / "data" / "a.parquet"
    seen: list[Path] = []
    sentinel = object()
    monkeypatch.setattr(
        dedup_module,
        "read_manifest",
        lambda path: seen.append(path) or sentinel,
    )

    result = _read_manifests(tmp_path, (parquet,))

    assert result == {"a.parquet": sentinel}
    assert seen == [tmp_path / "manifests" / "a.manifest.json"]


def test_resume_staged_promotes_state_and_returns_deduplicated_result(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    data_root = tmp_path / "generated"
    data_dir = data_root / "data"
    data_dir.mkdir(parents=True)
    output = data_dir / "a.parquet"
    output.write_bytes(b"output")
    state_path = data_root / ".work" / "dedup-state.json"
    state = {
        "status": "staged",
        "stage_dir": ".work/dedup/token",
        "inputs": {"a.parquet": "input-sha"},
        "input_rows": 8,
        "output_rows": 6,
        "duplicate_rows": 2,
        "files": [{"parquet": "data/a.parquet", "parquet_sha256": "output-sha"}],
    }
    calls: list[tuple[Path, Mapping[str, object], object]] = []
    writes: list[tuple[Path, dict[str, object]]] = []

    def promote(
        root: Path,
        actual_state: Mapping[str, object],
        *,
        promotion_hook: object = None,
    ) -> None:
        calls.append((root, actual_state, promotion_hook))

    monkeypatch.setattr(dedup_module, "_promote_staged", promote)
    monkeypatch.setattr(
        dedup_module,
        "_write_state",
        lambda path, payload: writes.append((path, dict(payload))),
    )
    hashed_paths: list[Path] = []
    monkeypatch.setattr(
        dedup_module,
        "_input_hashes",
        lambda paths: hashed_paths.extend(paths) or {"a.parquet": "output-sha"},
    )
    hook = object()

    result = _resume_staged(data_root, state_path, state, promotion_hook=hook)  # type: ignore[arg-type]

    assert calls == [(data_root, state, hook)]
    assert writes[0][0] == state_path
    assert writes[0][1]["status"] == "complete"
    assert "stage_dir" not in writes[0][1]
    assert writes[0][1]["outputs"] == {"a.parquet": "output-sha"}
    # Promotion reuses the verified and staged hashes instead of rereading files.
    assert hashed_paths == [output]
    assert result == DeduplicationResult("deduplicated", 8, 6, 2, 1)


def test_promoted_output_hashes_prefer_staged_hashes_and_sort_names() -> None:
    current = {"b.parquet": "b-current", "a.parquet": "a-input"}
    staged = {"b.parquet": "b-staged"}

    outputs = dedup_module._promoted_output_hashes(current, staged)

    assert outputs == {"a.parquet": "a-input", "b.parquet": "b-staged"}
    assert list(outputs) == ["a.parquet", "b.parquet"]


def test_resume_staged_accepts_state_without_a_stage_directory(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    data_root = tmp_path / "generated"
    output = data_root / "data" / "a.parquet"
    output.parent.mkdir(parents=True)
    output.write_bytes(b"output")
    state = {
        "inputs": {"a.parquet": "input-sha"},
        "input_rows": 2,
        "output_rows": 1,
        "duplicate_rows": 1,
        "files": [{"parquet": "data/a.parquet", "parquet_sha256": "output-sha"}],
    }
    writes: list[dict[str, object]] = []
    monkeypatch.setattr(dedup_module, "_promote_staged", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(
        dedup_module,
        "_input_hashes",
        lambda _paths: {"a.parquet": "output-sha"},
    )
    monkeypatch.setattr(
        dedup_module,
        "_write_state",
        lambda _path, payload: writes.append(dict(payload)),
    )

    result = _resume_staged(data_root, tmp_path / "state.json", state)

    assert "stage_dir" not in writes[0]
    assert result == DeduplicationResult("deduplicated", 2, 1, 1, 1)


def test_deduplicate_dataset_forwards_promotion_hook_when_resuming_staged(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    state = {"status": "staged"}
    result = DeduplicationResult("deduplicated", 2, 1, 1, 1)
    calls: list[tuple[Path, Path, Mapping[str, object], object]] = []
    hook = object()

    def read_state(path: Path) -> dict[str, object]:
        assert path == tmp_path / _STATE_RELATIVE_PATH
        return state

    def resume(
        root: Path,
        state_path: Path,
        actual_state: Mapping[str, object],
        *,
        promotion_hook: object = None,
    ) -> DeduplicationResult:
        calls.append((root, state_path, actual_state, promotion_hook))
        return result

    monkeypatch.setattr(dedup_module, "_read_state", read_state)
    monkeypatch.setattr(dedup_module, "_resume_staged", resume)

    assert deduplicate_dataset(tmp_path, promotion_hook=hook) is result
    assert calls == [(tmp_path, tmp_path / _STATE_RELATIVE_PATH, state, hook)]


def test_deduplicate_dataset_preserves_state_and_uses_stable_stage_path(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    state = {"status": "pending"}
    context = object()
    result = object()
    seen: dict[str, object] = {}

    monkeypatch.setattr(dedup_module, "_read_state", lambda _path: state)

    def prepare(
        root: Path, state_path: Path, actual_state: Mapping[str, object]
    ) -> tuple[object, None]:
        seen["prepare"] = (root, state_path, actual_state)
        return context, None

    monkeypatch.setattr(dedup_module, "_prepare_context", prepare)
    monkeypatch.setattr(
        dedup_module,
        "uuid",
        SimpleNamespace(uuid4=lambda: SimpleNamespace(hex="fixed-token")),
    )
    monkeypatch.setattr(
        dedup_module,
        "_stage_changes",
        lambda actual_context, stage_root: (
            seen.update(stage=(actual_context, stage_root)) or ([], 4)
        ),
    )
    monkeypatch.setattr(
        dedup_module,
        "_state_payload",
        lambda actual_context, changed, output_rows: (
            seen.update(payload=(actual_context, changed, output_rows)) or {"status": "complete"}
        ),
    )
    monkeypatch.setattr(
        dedup_module,
        "_finish_deduplication",
        lambda actual_context, stage_dir, payload, changed, output_rows, promotion_hook: (
            seen.update(
                finish=(
                    actual_context,
                    stage_dir,
                    payload,
                    changed,
                    output_rows,
                    promotion_hook,
                )
            )
            or result
        ),
    )

    assert deduplicate_dataset(tmp_path) is result
    assert seen["prepare"] == (tmp_path, tmp_path / _STATE_RELATIVE_PATH, state)
    assert seen["stage"] == (context, tmp_path / ".work" / "dedup" / "fixed-token")
    assert seen["payload"] == (context, [], 4)
    assert seen["finish"] == (
        context,
        Path(".work") / "dedup" / "fixed-token",
        {"status": "complete"},
        [],
        4,
        None,
    )


def test_deduplicate_dataset_rejects_missing_context(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setattr(dedup_module, "_read_state", lambda _path: None)
    monkeypatch.setattr(dedup_module, "_prepare_context", lambda *_args: (None, None))

    with pytest.raises(
        DeduplicationError,
        match=r"^deduplication context was not created$",
    ):
        deduplicate_dataset(tmp_path)


def test_prepare_context_returns_cached_result_without_reading_manifests(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    parquet = tmp_path / "data" / "a.parquet"
    state = {"status": "complete"}
    cached = DeduplicationResult("skipped", 1, 1, 0, 0)
    monkeypatch.setattr(dedup_module, "_validated_parquets", lambda _root: (parquet,))
    monkeypatch.setattr(dedup_module, "_input_hashes", lambda _paths: {"a.parquet": "sha"})

    def complete_result(
        actual_state: object, inputs: object, parquets: object
    ) -> DeduplicationResult:
        assert actual_state is state
        assert inputs == {"a.parquet": "sha"}
        assert parquets == (parquet,)
        return cached

    monkeypatch.setattr(dedup_module, "_complete_result", complete_result)
    monkeypatch.setattr(
        dedup_module,
        "_read_manifests",
        lambda *_args: pytest.fail("manifests must not be read for a cached result"),
    )

    context, result = _prepare_context(tmp_path, tmp_path / "state.json", state)

    assert context is None
    assert result is cached


def test_prepare_context_preserves_incomplete_state_in_context(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    parquet = tmp_path / "data" / "a.parquet"
    state = {"status": "staged"}
    manifests = {"a.parquet": SimpleNamespace(source=SimpleNamespace(name="a.osm.pbf"))}
    monkeypatch.setattr(dedup_module, "_validated_parquets", lambda _root: (parquet,))
    monkeypatch.setattr(dedup_module, "_input_hashes", lambda _paths: {"a.parquet": "sha"})
    monkeypatch.setattr(dedup_module, "_complete_result", lambda *_args: None)
    monkeypatch.setattr(dedup_module, "_read_manifests", lambda *_args: manifests)
    monkeypatch.setattr(dedup_module, "_current_output_rows", lambda _paths: 4)

    context, result = _prepare_context(tmp_path, tmp_path / "state.json", state)

    assert result is None
    assert context is not None
    assert context.state is state
    assert context.input_rows == 4
    assert context.manifests == manifests


def test_stage_changes_accumulates_rows_across_all_parquets(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    first = tmp_path / "a.parquet"
    second = tmp_path / "b.parquet"
    context = _DeduplicationContext(
        data_root=tmp_path,
        state_path=tmp_path / "state.json",
        state=None,
        parquets=(first, second),
        manifests={
            "a.parquet": SimpleNamespace(source=SimpleNamespace(name="a.osm.pbf")),
            "b.parquet": SimpleNamespace(source=SimpleNamespace(name="b.osm.pbf")),
        },
        inputs={},
        input_rows=8,
    )
    calls: list[str] = []

    class Connection:
        def close(self) -> None:
            calls.append("close")

    monkeypatch.setattr(dedup_module.duckdb, "connect", lambda: Connection())
    monkeypatch.setattr(dedup_module, "_canonical_relation", lambda *_args: None)
    monkeypatch.setattr(dedup_module, "_assert_known_sources", lambda *_args: None)

    def stage(
        _connection: object, parquet: Path, _manifest: object, _root: Path
    ) -> tuple[int, dict[str, object] | None]:
        calls.append(parquet.name)
        return (3, {"parquet": "data/a.parquet"}) if parquet == first else (5, None)

    monkeypatch.setattr(dedup_module, "_stage_source", stage)

    changed, output_rows = _stage_changes(context, tmp_path / "stage")

    assert changed == [{"parquet": "data/a.parquet"}]
    assert output_rows == 8
    assert calls == ["a.parquet", "b.parquet", "close"]


def test_complete_result_falls_back_to_current_output_rows_when_input_rows_missing(
    tmp_path: Path,
) -> None:
    parquet = tmp_path / "a.parquet"
    write_geoparquet(_two_records(), parquet)
    inputs = {"a.parquet": file_sha256(parquet)}
    state = {
        "status": "complete",
        "policy_sha256": DEDUPLICATION_POLICY_SHA256,
        "outputs": inputs,
    }

    assert _complete_result(state, inputs, (parquet,)) == _skipped_result(2, 2, 0)


def test_complete_result_returns_none_without_state(tmp_path: Path) -> None:
    assert _complete_result(None, {}, ()) is None


@pytest.mark.parametrize(
    ("input_rows", "output_rows", "duplicate_rows"),
    [(0, 0, 0), (8, 6, 2)],
)
def test_skipped_result_builds_machine_readable_result(
    input_rows: int, output_rows: int, duplicate_rows: int
) -> None:
    assert _skipped_result(input_rows, output_rows, duplicate_rows) == DeduplicationResult(
        "skipped", input_rows, output_rows, duplicate_rows, 0
    )


def test_skipped_result_defaults_all_row_counts_to_zero() -> None:
    assert _skipped_result() == DeduplicationResult("skipped", 0, 0, 0, 0)


def test_select_canonical_row_rejects_empty_groups() -> None:
    with pytest.raises(
        ValueError,
        match=r"^cannot select a canonical row from an empty group$",
    ):
        select_canonical_row([])


def test_parse_timestamp_accepts_zulu_suffix_and_normalizes_it_explicitly() -> None:
    """Keep the explicit Zulu compatibility normalization independent of parser version."""
    replacements: list[tuple[str, str]] = []

    class _ZuluTimestamp(str):
        def replace(self, old: str, new: str, count: int = -1) -> str:
            replacements.append((old, new))
            return super().replace(old, new, count)

    parsed = canonical_rows._parse_timestamp(_ZuluTimestamp("2026-01-01T00:00:00Z"))

    assert parsed is not None
    assert parsed.isoformat() == "2026-01-01T00:00:00+00:00"
    assert replacements == [("Z", "+00:00")]


def test_timestamp_rank_normalizes_to_utc_before_epoch_conversion(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    seen_timezones: list[object] = []

    class _ParsedTimestamp:
        tzinfo = UTC

        def astimezone(self, timezone: object) -> object:
            seen_timezones.append(timezone)
            return self

        def timestamp(self) -> float:
            return 42.0

    monkeypatch.setattr(canonical_rows, "_parse_timestamp", lambda _value: _ParsedTimestamp())

    assert canonical_rows._timestamp_rank("ignored") == 42.0
    assert seen_timezones == [UTC]


def test_row_fingerprint_uses_explicit_lowercase_utf8_encoding(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The canonical byte encoding is an explicit deterministic policy invariant."""
    encodings: list[tuple[str, str]] = []

    class _Payload(str):
        def encode(self, encoding: str = "utf-8", errors: str = "strict") -> bytes:
            encodings.append((encoding, errors))
            return super().encode(encoding, errors)

    monkeypatch.setattr(canonical_rows.json, "dumps", lambda *_args, **_kwargs: _Payload("x"))

    canonical_rows._row_fingerprint({})

    assert encodings == [("utf-8", "strict")]


def test_canonical_row_order_sql_has_stable_keyword_casing() -> None:
    """SQL keyword spelling is kept stable even where DuckDB treats case as equivalent."""
    order = canonical_rows.canonical_row_order_sql()

    assert order.split(", sha256", 1)[0] == (
        "version DESC NULLS LAST, timestamp DESC NULLS LAST, source_pbf ASC"
    )
    # The default decides how the tie-breaking fingerprint reads key/value
    # columns, and the two spellings order rows differently, so the default
    # itself is pinned rather than only the part of the clause it cannot reach.
    assert order == canonical_rows.canonical_row_order_sql(key_value_columns_are_maps=False)
    assert order != canonical_rows.canonical_row_order_sql(key_value_columns_are_maps=True)


def test_the_text_flag_view_rejects_an_empty_selection() -> None:
    with pytest.raises(ValueError) as empty:
        canonical_rows.canonical_rows_with_text_flag_sql("rows", ())
    assert str(empty.value) == "unique-row views require at least one selected column"


def test_canonical_rows_sql_preserves_selection_validation_contract() -> None:
    with pytest.raises(ValueError) as empty:
        canonical_rows.canonical_rows_sql("rows", ())
    assert str(empty.value) == "unique-row views require at least one selected column"

    with pytest.raises(ValueError) as unknown:
        canonical_rows.canonical_rows_sql("rows", ("not_a_schema_column",))
    assert str(unknown.value) == ("unsupported unique-row columns: ['not_a_schema_column']")


def test_canonical_geometry_wkb_forwards_every_stable_encoding_option(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    geometry = object()
    seen: dict[str, object] = {}

    monkeypatch.setattr(canonical_rows, "from_wkb", lambda _value: geometry)

    def encode(actual: object, **options: object) -> bytes:
        seen["geometry"] = actual
        seen["options"] = options
        return b"canonical"

    monkeypatch.setattr(canonical_rows, "to_wkb", encode)

    assert canonical_rows.canonical_geometry_wkb(b"input") == b"canonical"
    assert seen == {
        "geometry": geometry,
        "options": {"byte_order": 1, "include_srid": False, "output_dimension": 2},
    }


def test_non_geometry_bytes_are_not_interpreted_as_wkb() -> None:
    assert canonical_rows._fingerprint_value("name", b"opaque") == b"opaque"


def test_promote_artifact_moves_staged_file_and_reuses_identical_target(tmp_path: Path) -> None:
    stage = tmp_path / "stage"
    root = tmp_path / "root"
    staged = stage / "data" / "a.parquet"
    target = root / "data" / "a.parquet"
    staged.parent.mkdir(parents=True)
    staged.write_bytes(b"payload")
    expected_sha = hashlib.sha256(b"payload").hexdigest()

    assert _promote_artifact(stage, root, "data/a.parquet", expected_sha) is True
    assert target.read_bytes() == b"payload"
    assert _promote_artifact(stage, root, "data/a.parquet", expected_sha) is False

    with pytest.raises(DeduplicationError, match="missing staged artifact"):
        _promote_artifact(stage, root, "data/missing.parquet", "0" * 64)


def test_promote_artifact_allows_an_existing_target_parent(tmp_path: Path) -> None:
    stage = tmp_path / "stage"
    root = tmp_path / "root"
    staged = stage / "nested" / "a.parquet"
    (root / "nested").mkdir(parents=True)
    staged.parent.mkdir(parents=True)
    staged.write_bytes(b"payload")

    assert (
        _promote_artifact(
            stage,
            root,
            "nested/a.parquet",
            hashlib.sha256(b"payload").hexdigest(),
        )
        is True
    )


def test_promote_entry_reuses_identical_targets_when_staged_files_are_absent(
    tmp_path: Path,
) -> None:
    stage = tmp_path / "stage"
    root = tmp_path / "root"
    parquet = root / "data" / "a.parquet"
    manifest = root / "manifests" / "a.manifest.json"
    parquet.parent.mkdir(parents=True)
    manifest.parent.mkdir(parents=True)
    parquet.write_bytes(b"parquet")
    manifest.write_bytes(b"manifest")
    entry = {
        "parquet": "data/a.parquet",
        "manifest": "manifests/a.manifest.json",
        "parquet_sha256": file_sha256(parquet),
        "manifest_sha256": file_sha256(manifest),
    }

    assert _promote_entry(stage, root, entry, promotion_hook=None, promoted=0) == 0


def test_promote_entry_moves_both_files_and_reports_each_promotion(tmp_path: Path) -> None:
    stage = tmp_path / "stage"
    root = tmp_path / "root"
    parquet = stage / "data" / "a.parquet"
    manifest = stage / "manifests" / "a.manifest.json"
    parquet.parent.mkdir(parents=True)
    manifest.parent.mkdir(parents=True)
    parquet.write_bytes(b"parquet")
    manifest.write_bytes(b"manifest")
    entry = {
        "parquet": "data/a.parquet",
        "manifest": "manifests/a.manifest.json",
        "parquet_sha256": file_sha256(parquet),
        "manifest_sha256": file_sha256(manifest),
    }
    promoted: list[int] = []

    count = _promote_entry(
        stage,
        root,
        entry,
        promotion_hook=promoted.append,
        promoted=0,
    )

    assert count == 2
    assert promoted == [1, 2]
    assert (root / "data" / "a.parquet").read_bytes() == b"parquet"
    assert (root / "manifests" / "a.manifest.json").read_bytes() == b"manifest"


def test_promote_staged_removes_stage_after_all_entries(tmp_path: Path) -> None:
    root = tmp_path / "root"
    stage = root / ".work" / "dedup" / "token"
    parquet = stage / "data" / "a.parquet"
    manifest = stage / "manifests" / "a.manifest.json"
    parquet.parent.mkdir(parents=True)
    manifest.parent.mkdir(parents=True)
    parquet.write_bytes(b"parquet")
    manifest.write_bytes(b"manifest")
    state = {
        "stage_dir": ".work/dedup/token",
        "files": [
            {
                "parquet": "data/a.parquet",
                "manifest": "manifests/a.manifest.json",
                "parquet_sha256": file_sha256(parquet),
                "manifest_sha256": file_sha256(manifest),
            }
        ],
    }
    progress: list[int] = []

    _promote_staged(root, state, promotion_hook=progress.append)

    assert progress == [1, 2]
    assert not stage.exists()


def test_promote_staged_rejects_a_missing_stage_directory(tmp_path: Path) -> None:
    with pytest.raises(
        DeduplicationError,
        match=r"^staged deduplication directory is missing: .+missing$",
    ):
        _promote_staged(
            tmp_path / "root",
            {"stage_dir": ".work/dedup/missing", "files": []},
        )


def test_state_payload_records_policy_inputs_and_duplicate_delta(tmp_path: Path) -> None:
    context = _DeduplicationContext(
        data_root=tmp_path,
        state_path=tmp_path / "state.json",
        state=None,
        parquets=(),
        manifests={},
        inputs={"z.parquet": "z", "a.parquet": "a"},
        input_rows=7,
    )
    changed = [{"parquet": "data/a.parquet"}]

    payload = _state_payload(context, changed, 5)

    assert payload == {
        "schema_version": 1,
        "policy_version": DEDUPLICATION_POLICY_VERSION,
        "policy_sha256": DEDUPLICATION_POLICY_SHA256,
        "status": "staged",
        "inputs": {"a.parquet": "a", "z.parquet": "z"},
        "outputs": {},
        "input_rows": 7,
        "output_rows": 5,
        "duplicate_rows": 2,
        "files": changed,
    }


def test_state_payload_marks_an_unchanged_pass_complete_and_preserves_outputs(
    tmp_path: Path,
) -> None:
    context = _DeduplicationContext(
        data_root=tmp_path,
        state_path=tmp_path / "state.json",
        state=None,
        parquets=(),
        manifests={},
        inputs={"b.parquet": "b", "a.parquet": "a"},
        input_rows=4,
    )

    payload = _state_payload(context, [], 4)

    assert payload["status"] == "complete"
    assert payload["outputs"] == {"a.parquet": "a", "b.parquet": "b"}
    assert payload["duplicate_rows"] == 0


def test_complete_result_reuses_complete_state_and_current_output_count(tmp_path: Path) -> None:
    parquet = tmp_path / "a.parquet"
    write_geoparquet(_two_records(), parquet)
    inputs = {"a.parquet": file_sha256(parquet)}
    state = {
        "status": "complete",
        "policy_sha256": DEDUPLICATION_POLICY_SHA256,
        "outputs": inputs,
        "input_rows": 3,
        "duplicate_rows": 1,
    }

    result = _complete_result(state, inputs, (parquet,))

    assert result is not None
    assert result.status == "skipped"
    assert result.input_rows == 3
    assert result.output_rows == 2
    assert result.duplicate_rows == 1


def test_finish_deduplication_writes_complete_state_without_changes(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    context = _DeduplicationContext(
        data_root=tmp_path,
        state_path=tmp_path / "state.json",
        state=None,
        parquets=(),
        manifests={},
        inputs={"a.parquet": "sha"},
        input_rows=2,
    )
    writes: list[tuple[Path, dict[str, object]]] = []
    monkeypatch.setattr(
        dedup_module,
        "_write_state",
        lambda path, payload: writes.append((path, dict(payload))),
    )

    result = _finish_deduplication(context, tmp_path / "stage", {}, [], 2, None)

    assert result.status == "skipped"
    assert result.input_rows == 2
    assert result.output_rows == 2
    assert writes == [(context.state_path, {"outputs": {"a.parquet": "sha"}})]


def test_the_canonical_relation_sql_is_pinned_text() -> None:
    """DuckDB ignores keyword and identifier case, so behaviour cannot pin this.

    The relation is a deterministic artifact of this module, and its text is
    what a reviewer reads when a deduplication result is questioned, so the
    exact SQL is the contract.
    """
    executed: list[str] = []

    class Connection:
        def execute(self, sql: str) -> None:
            executed.append(sql)

    _canonical_relation(Connection(), [Path("/data/a.parquet")])

    assert len(executed) == 1
    assert executed[0].startswith("CREATE TEMP TABLE deduplicated AS ")
    assert "(SELECT * EXCLUDE (geometry), " in executed[0]
    assert "AS geometry FROM read_parquet(['/data/a.parquet'])" in executed[0]
