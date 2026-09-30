"""Additive language-v1 export, allowlisting, and guarded publication."""

import hashlib
import json
from collections.abc import Sequence
from dataclasses import replace
from pathlib import Path
from typing import cast

import pyarrow as pa
import pyarrow.parquet as pq
import pytest
from shapely.geometry import Polygon

from osm_polygon_description_tag.dataset.languages.checkpoint import (
    part_name_for_offset,
    shard_paths,
)
from osm_polygon_description_tag.dataset.languages.models import (
    LanguageResult,
    LanguageStatus,
)
from osm_polygon_description_tag.dataset.languages.snapshot import (
    prepare_snapshot,
)
from osm_polygon_description_tag.dataset.languages.worker import process_shard
from osm_polygon_description_tag.dataset.storage import write_geoparquet
from osm_polygon_description_tag.publication import language as language_module
from osm_polygon_description_tag.publication import language_upload as language_upload_module
from osm_polygon_description_tag.publication.language import (
    LANGUAGE_STATS_PATH,
    LanguageExport,
    LanguagePublicationError,
    build_language_upload_plan,
    export_language_annotations,
    read_language_export,
    render_language_card_section,
)
from osm_polygon_description_tag.publication.language_upload import (
    PublicationOutcome,
    PublishStatus,
    RemoteFile,
    publish_language_export,
    read_language_publication_state,
    verify_language_publication,
)
from osm_polygon_description_tag.publication.models import UploadItem, UploadPlan
from osm_polygon_description_tag.runtime.logging import RunLogger
from tests.conftest import make_record_dict
from tests.helpers.language_publication import (
    REPO,
    SHARD,
)
from tests.helpers.language_publication import (
    FakeHub as _FakeHub,
)
from tests.helpers.language_publication import (
    language_export as _language_export_fixture,  # noqa: F401
)
from tests.helpers.language_publication import (
    process as _process,
)
from tests.helpers.language_publication import (
    run_directory as _language_run_dir_fixture,  # noqa: F401
)
from tests.helpers.language_publication import (
    write_shard as _write_shard,
)
from tests.helpers.messages import exactly
from tests.helpers.sentences import fake_splitter


def test_publish_language_export_uses_the_requested_resume_state_path(
    export: LanguageExport,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    plan = build_language_upload_plan(export, REPO, confirm_repo=REPO)
    state = tmp_path / "selected-state.json"
    observed: list[tuple[Path, UploadPlan]] = []
    original = language_upload_module._resumed_outcome

    def read_selected_state(path: Path, candidate: UploadPlan) -> PublicationOutcome | None:
        observed.append((path, candidate))
        return original(path, candidate)

    monkeypatch.setattr(language_upload_module, "_resumed_outcome", read_selected_state)
    language_upload_module._publish_language_export(
        plan,
        _FakeHub(),
        baseline_revision="rev-1",
        apply=True,
        state_path=state,
        logger=None,
    )

    assert observed == [(state, plan)]


@pytest.mark.parametrize(
    ("status", "expected_resumed"),
    [
        (PublishStatus.PLANNED, None),
        (PublishStatus.DRIFTED, None),
        (PublishStatus.VERIFIED, "recorded"),
    ],
)
def test_resumed_outcome_uses_only_a_matching_completed_state(
    export: LanguageExport,
    tmp_path: Path,
    status: PublishStatus,
    expected_resumed: str | None,
) -> None:
    plan = build_language_upload_plan(export, REPO, confirm_repo=REPO)
    state = tmp_path / "publication.json"
    recorded = PublicationOutcome(status, plan.repo_id, plan.identity_sha256, "rev-1", (), ())
    state.write_text(json.dumps(recorded.to_payload()), encoding="utf-8")

    resumed = language_upload_module._resumed_outcome(state, plan)

    assert resumed == (recorded if expected_resumed == "recorded" else None)


def test_a_failed_upload_is_logged_with_its_cause(export: LanguageExport, tmp_path: Path) -> None:
    plan = build_language_upload_plan(export, REPO, confirm_repo=REPO)
    events: list[tuple[str, str, dict[str, object]]] = []

    class _Logger:
        def event(self, name: str, *, level: str = "INFO", **fields: object) -> None:
            events.append((name, level, fields))

    publish_language_export(
        plan,
        _FakeHub(fail_upload=True),
        baseline_revision="rev-1",
        apply=True,
        state_path=tmp_path / "state.json",
        logger=cast(RunLogger, _Logger()),
    )

    assert events == [
        (
            "language_publication_upload_failed",
            "WARNING",
            {
                "result": "ambiguous",
                "reason": "RuntimeError: network died mid-upload",
                "identity_sha256": plan.identity_sha256,
            },
        )
    ]


def test_an_ambiguous_attempt_verifies_instead_of_re_uploading(
    export: LanguageExport, tmp_path: Path
) -> None:
    plan = build_language_upload_plan(export, REPO, confirm_repo=REPO)
    state = tmp_path / "state.json"
    publish_language_export(
        plan, _FakeHub(fail_upload=True), baseline_revision="rev-1", apply=True, state_path=state
    )

    hub = _FakeHub()
    for item in plan.files:
        hub.stored[item.relative_path] = RemoteFile(
            item.relative_path, item.size_bytes, item.sha256
        )

    outcome = publish_language_export(
        plan, hub, baseline_revision="rev-1", apply=True, state_path=state
    )

    assert outcome.status is PublishStatus.VERIFIED
    assert hub.uploads == []


def test_a_verified_publication_is_not_repeated(export: LanguageExport, tmp_path: Path) -> None:
    plan = build_language_upload_plan(export, REPO, confirm_repo=REPO)
    state = tmp_path / "state.json"
    hub = _FakeHub()
    publish_language_export(plan, hub, baseline_revision="rev-1", apply=True, state_path=state)

    second = publish_language_export(
        plan, hub, baseline_revision="rev-1", apply=True, state_path=state
    )

    assert second.status is PublishStatus.VERIFIED
    assert len(hub.uploads) == 1


def test_previously_verified_state_does_not_hide_remote_file_loss(
    export: LanguageExport, tmp_path: Path
) -> None:
    plan = build_language_upload_plan(export, REPO, confirm_repo=REPO)
    state = tmp_path / "state.json"
    hub = _FakeHub()
    publish_language_export(plan, hub, baseline_revision="rev-1", apply=True, state_path=state)
    del hub.stored[LANGUAGE_STATS_PATH]

    outcome = publish_language_export(
        plan, hub, baseline_revision="rev-1", apply=True, state_path=state
    )

    assert outcome.status is PublishStatus.UNVERIFIED
    assert len(hub.uploads) == 1


def test_a_remote_size_mismatch_is_not_reported_as_verified(export: LanguageExport) -> None:
    plan = build_language_upload_plan(export, REPO, confirm_repo=REPO)
    hub = _FakeHub(corrupt=LANGUAGE_STATS_PATH)

    outcome = publish_language_export(plan, hub, baseline_revision="rev-1", apply=True)

    assert outcome.status is PublishStatus.UNVERIFIED
    assert any("remote size differs" in issue for issue in outcome.issues)


def test_a_missing_remote_file_is_not_reported_as_verified(export: LanguageExport) -> None:
    plan = build_language_upload_plan(export, REPO, confirm_repo=REPO)
    hub = _FakeHub(missing=LANGUAGE_STATS_PATH)

    outcome = publish_language_export(plan, hub, baseline_revision="rev-1", apply=True)

    assert outcome.status is PublishStatus.UNVERIFIED
    assert any("remote file is missing" in issue for issue in outcome.issues)


def test_a_missing_viewer_configuration_is_reported(export: LanguageExport) -> None:
    plan = build_language_upload_plan(export, REPO, confirm_repo=REPO)
    hub = _FakeHub(configs=("default",))

    outcome = publish_language_export(plan, hub, baseline_revision="rev-1", apply=True)

    assert outcome.status is PublishStatus.UNVERIFIED
    assert any("Dataset Viewer does not expose" in issue for issue in outcome.issues)


def test_a_checksum_mismatch_is_reported(export: LanguageExport) -> None:
    plan = build_language_upload_plan(export, REPO, confirm_repo=REPO)
    hub = _FakeHub()
    hub.upload(plan)
    path = plan.files[0].relative_path
    hub.stored[path] = RemoteFile(path, plan.files[0].size_bytes, "f" * 64)

    verified, issues = verify_language_publication(plan, hub, revision="rev-1")

    assert path not in verified
    assert any("remote checksum differs" in issue for issue in issues)


def test_publication_state_round_trips(tmp_path: Path) -> None:
    outcome = PublicationOutcome(
        PublishStatus.VERIFIED, REPO, "a" * 64, "rev-1", ("language-v1/stats.json",), ()
    )
    state = tmp_path / "state.json"
    state.write_text(json.dumps(outcome.to_payload()), encoding="utf-8")

    assert read_language_publication_state(state) == outcome
    assert read_language_publication_state(tmp_path / "absent.json") is None


@pytest.mark.parametrize(
    ("mutation", "message"),
    [
        ({"state_schema_version": 2}, "unsupported publication state schema version"),
        ({"status": "invented"}, "unsupported publication status"),
        ({"revision": 7}, "revision must be a string or null"),
        ({"repo_id": None}, "must be a string"),
        ({"verified_files": [7]}, "must contain only strings"),
    ],
)
def test_a_malformed_publication_state_is_rejected(
    tmp_path: Path, mutation: dict[str, object], message: str
) -> None:
    outcome = PublicationOutcome(PublishStatus.VERIFIED, REPO, "a" * 64, "rev-1", (), ())
    state = tmp_path / "state.json"
    state.write_text(json.dumps({**outcome.to_payload(), **mutation}), encoding="utf-8")

    with pytest.raises(LanguagePublicationError, match=message):
        read_language_publication_state(state)


def test_an_unreadable_publication_state_is_reported(tmp_path: Path) -> None:
    state = tmp_path / "state.json"
    state.write_text("{not-json", encoding="utf-8")

    with pytest.raises(LanguagePublicationError, match="cannot read publication state"):
        read_language_publication_state(state)


def test_a_state_for_another_plan_does_not_resume(export: LanguageExport, tmp_path: Path) -> None:
    plan = build_language_upload_plan(export, REPO, confirm_repo=REPO)
    state = tmp_path / "state.json"
    state.write_text(
        json.dumps(
            PublicationOutcome(PublishStatus.VERIFIED, REPO, "0" * 64, "rev-1", (), ()).to_payload()
        ),
        encoding="utf-8",
    )
    hub = _FakeHub()

    outcome = publish_language_export(
        plan, hub, baseline_revision="rev-1", apply=True, state_path=state
    )

    assert outcome.status is PublishStatus.VERIFIED
    assert len(hub.uploads) == 1


@pytest.mark.parametrize("status", [PublishStatus.AMBIGUOUS, PublishStatus.UNVERIFIED])
def test_unresolved_other_plan_cannot_be_overwritten(
    export: LanguageExport, tmp_path: Path, status: PublishStatus
) -> None:
    plan = build_language_upload_plan(export, REPO, confirm_repo=REPO)
    state = tmp_path / "state.json"
    original = json.dumps(PublicationOutcome(status, REPO, "0" * 64, "rev-1", (), ()).to_payload())
    state.write_text(original, encoding="utf-8")
    hub = _FakeHub()

    with pytest.raises(
        LanguagePublicationError,
        match=exactly("an unresolved publication belongs to another plan"),
    ):
        publish_language_export(plan, hub, baseline_revision="rev-1", apply=True, state_path=state)

    assert hub.uploads == []
    assert state.read_text(encoding="utf-8") == original


def test_a_shard_name_that_cannot_be_expressed_as_a_file_is_rejected(tmp_path: Path) -> None:
    source = tmp_path / "source"
    _write_shard(source / "___.parquet", 3)
    run = tmp_path / "run"
    snapshot = prepare_snapshot(source, run, code_fingerprint="a" * 64, lock_fingerprint="b" * 64)
    _process(source, run, snapshot, "___.parquet")

    with pytest.raises(LanguagePublicationError, match="cannot be expressed as a file name"):
        export_language_annotations(run, tmp_path / "export")


def test_two_shards_that_would_export_to_one_file_are_rejected(tmp_path: Path) -> None:
    source = tmp_path / "source"
    _write_shard(source / "a-b.parquet", 3)
    _write_shard(source / "a_b.parquet", 3, start=50)
    run = tmp_path / "run"
    snapshot = prepare_snapshot(source, run, code_fingerprint="a" * 64, lock_fingerprint="b" * 64)
    _process(source, run, snapshot, "a-b.parquet")
    _process(source, run, snapshot, "a_b.parquet")

    with pytest.raises(LanguagePublicationError, match="export to the same file name"):
        export_language_annotations(run, tmp_path / "export")


def test_an_export_round_trips_through_its_stats_file(export: LanguageExport) -> None:
    assert read_language_export(export.export_root) == export


def test_an_unreadable_export_is_reported(tmp_path: Path) -> None:
    with pytest.raises(LanguagePublicationError, match="cannot read language export"):
        read_language_export(tmp_path / "absent")

    root = tmp_path / "export"
    (root / "language-v1").mkdir(parents=True)
    (root / LANGUAGE_STATS_PATH).write_text("{not-json", encoding="utf-8")
    with pytest.raises(LanguagePublicationError, match="cannot read language export"):
        read_language_export(root)


def _rewrite_export(export: LanguageExport, mutate: object) -> None:
    path = export.export_root / LANGUAGE_STATS_PATH
    payload = json.loads(path.read_text(encoding="utf-8"))
    mutate(payload)  # type: ignore[operator]
    path.write_text(json.dumps(payload), encoding="utf-8")


def test_an_export_declaring_another_configuration_is_rejected(export: LanguageExport) -> None:
    _rewrite_export(export, lambda payload: payload.__setitem__("config_name", "language-v2"))

    with pytest.raises(LanguagePublicationError, match="different configuration name"):
        read_language_export(export.export_root)


def test_an_export_with_a_stale_annotation_schema_is_rejected(export: LanguageExport) -> None:
    _rewrite_export(
        export, lambda payload: payload["stats"].__setitem__("annotation_schema_version", 9)
    )

    with pytest.raises(
        LanguagePublicationError,
        match=exactly("unsupported annotation schema version in export stats"),
    ):
        read_language_export(export.export_root)


def test_a_traversing_upload_path_is_rejected(export: LanguageExport) -> None:
    traversing = replace(export, files=("language-v1/../../etc/passwd",))

    with pytest.raises(LanguagePublicationError, match="must not traverse"):
        build_language_upload_plan(traversing, REPO, confirm_repo=REPO)


def test_the_plan_identity_covers_the_card_section_it_will_commit(
    export: LanguageExport, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A changed card must be a different publication, or it can never ship.

    ``upload`` commits the data files *and* the rendered card section in one
    commit, but the identity was computed over the files alone. Correcting the
    card while the data stayed byte-identical therefore produced the same
    identity, `publish` resumed its recorded outcome, skipped the upload
    entirely, and the corrected card could never reach the Hub.
    """
    before = build_language_upload_plan(export, REPO, confirm_repo=REPO).identity_sha256

    monkeypatch.setattr(
        language_module, "render_language_card_section", lambda _export: "## rewritten\n"
    )
    after = build_language_upload_plan(export, REPO, confirm_repo=REPO).identity_sha256

    assert before != after


def test_the_plan_identity_is_stable_when_nothing_changes(export: LanguageExport) -> None:
    """Identity must still be a pure function of what gets committed."""
    first = build_language_upload_plan(export, REPO, confirm_repo=REPO)
    second = build_language_upload_plan(export, REPO, confirm_repo=REPO)

    assert first.identity_sha256 == second.identity_sha256


@pytest.fixture
def completed_export(tmp_path: Path) -> LanguageExport:
    source, run = tmp_path / "source", tmp_path / "run"
    source.mkdir()
    write_geoparquet(
        [
            make_record_dict(
                Polygon([(0, 0), (0, 1), (1, 1), (1, 0)]),
                {"description": "Synthetic export text"},
                osm_id=1,
            )
        ],
        source / SHARD,
    )
    snapshot = prepare_snapshot(source, run, code_fingerprint="a" * 64, lock_fingerprint="b" * 64)
    process_shard(
        run,
        source,
        SHARD,
        snapshot=snapshot,
        detector=lambda _text: LanguageResult(
            "eng", 0.9, 0.1, 0.8, LanguageStatus.DETECTED, "detected"
        ),
        splitter=fake_splitter(),
    )
    return export_language_annotations(run, tmp_path / "export-é")


def test_parquet_export_bounds_batches_and_preserves_zstd_and_rows(
    completed_export: LanguageExport, tmp_path: Path
) -> None:
    seed = pq.read_table(completed_export.export_root / completed_export.files[0]).slice(0, 1)
    table = pa.concat_tables([seed] * 4097).combine_chunks()
    paths = shard_paths(tmp_path / "parts-run", SHARD)
    part = paths.part(part_name_for_offset(0))
    part.parent.mkdir(parents=True, exist_ok=True)
    pq.write_table(table, part, row_group_size=4097)
    target = tmp_path / "bounded.parquet"
    stats = language_module._StatsAccumulator()

    language_module._write_shard_export(target, paths, (part.name,), stats)

    parquet = pq.ParquetFile(target)
    assert [parquet.metadata.row_group(i).num_rows for i in range(parquet.num_row_groups)] == [
        4096,
        1,
    ]
    assert {
        parquet.metadata.row_group(i).column(j).compression
        for i in range(parquet.num_row_groups)
        for j in range(parquet.metadata.num_columns)
    } == {"ZSTD"}
    assert parquet.read().equals(table)
    assert stats.result().annotation_count == 4097


def test_plan_identity_covers_exact_root_repository_and_file_bytes(
    completed_export: LanguageExport,
) -> None:
    root = completed_export.export_root
    names = (
        "language-v1/data/region.parquet",
        "language-v1/export-manifest.json",
        "language-v1/stats.json",
    )
    items = tuple(
        UploadItem(
            name,
            len((root / name).read_bytes()),
            hashlib.sha256((root / name).read_bytes()).hexdigest(),
        )
        for name in names
    )
    payload = {
        "repo_id": "owner/export-é",
        "data_root": str(root),
        "files": [
            {
                "relative_path": item.relative_path,
                "size_bytes": item.size_bytes,
                "sha256": item.sha256,
            }
            for item in items
        ],
    }
    encoded = (json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n").encode()
    # The rendered card ships in the same commit as the files, so it is part of
    # the identity too; without it a corrected card is indistinguishable from
    # the published one and can never be uploaded.
    encoded += render_language_card_section(completed_export).encode()

    plan = build_language_upload_plan(
        completed_export, "owner/export-é", confirm_repo="owner/export-é"
    )

    assert plan == UploadPlan(
        "owner/export-é", str(root), items, hashlib.sha256(encoded).hexdigest()
    )


def test_plan_identity_changes_with_repository_root_or_file_identity(tmp_path: Path) -> None:
    item = UploadItem("language-v1/stats.json", 10, "a" * 64)
    variants = [
        ("owner/one", tmp_path, (item,)),
        ("owner/two", tmp_path, (item,)),
        ("owner/one", tmp_path / "other", (item,)),
        ("owner/one", tmp_path, (UploadItem(item.relative_path, 11, item.sha256),)),
        ("owner/one", tmp_path, (UploadItem(item.relative_path, 10, "b" * 64),)),
        ("owner/one", tmp_path, (UploadItem("language-v1/other.json", 10, item.sha256),)),
    ]
    plans = [
        language_module._identified_plan(repo, root, files, "## card\n")
        for repo, root, files in variants
    ]

    assert len({plan.identity_sha256 for plan in plans}) == len(variants)
    # The card is held constant here, so any difference comes from the repo,
    # the root or the files -- which is what this test is about.
    assert (
        language_module._identified_plan(*variants[0], "## other\n").identity_sha256
        != plans[0].identity_sha256
    )
    for plan, (repo, root, files) in zip(plans, variants, strict=True):
        assert (plan.repo_id, plan.data_root, plan.files) == (repo, str(root), files)
        assert len(plan.identity_sha256) == 64


@pytest.mark.parametrize(
    ("field", "value", "message"),
    [
        ("state_schema_version", 2, "unsupported publication state schema version"),
        ("revision", 7, "publication state revision must be a string or null"),
        ("status", "invalid", "unsupported publication status: 'invalid'"),
        ("issues", [7], "publication state field issues must contain only strings"),
    ],
)
def test_state_payload_diagnostics_identify_the_invalid_field(
    field: str, value: object, message: str
) -> None:
    payload = {
        "state_schema_version": 1,
        "status": "planned",
        "repo_id": "owner/dataset",
        "plan_identity": "a" * 64,
        "revision": None,
        "verified_files": [],
        "issues": [],
    }
    payload[field] = value
    with pytest.raises(LanguagePublicationError) as caught:
        PublicationOutcome.from_payload(payload)
    assert str(caught.value) == message


@pytest.mark.parametrize(
    ("entry", "message"),
    [
        (None, "top language payload must be an object"),
        (
            {"language_code": 7, "annotation_count": 1},
            "top language field language_code must be a string",
        ),
        (
            {"language_code": "eng", "annotation_count": True},
            "top language field annotation_count must be an integer",
        ),
        ({"language_code": "eng"}, "top language payload is missing annotation_count"),
    ],
)
def test_malformed_top_language_reports_the_exact_field(
    completed_export: LanguageExport, entry: object, message: str
) -> None:
    payload = completed_export.to_payload()
    payload["stats"]["top_languages"] = [entry]  # type: ignore[index]
    (completed_export.export_root / "language-v1/stats.json").write_text(json.dumps(payload))

    with pytest.raises(LanguagePublicationError) as caught:
        read_language_export(completed_export.export_root)
    assert str(caught.value) == message


@pytest.mark.parametrize(
    ("payload", "message"),
    [
        ([], "language export payload must be an object"),
        ({"config_name": 7}, "language export field config_name must be a string"),
        ({"config_name": "other"}, "export declares a different configuration name"),
        ({"config_name": "language-v1"}, "language export payload is missing snapshot_id"),
    ],
)
def test_malformed_export_has_an_exact_document_diagnostic(
    tmp_path: Path, payload: object, message: str
) -> None:
    (tmp_path / "language-v1").mkdir()
    (tmp_path / "language-v1/stats.json").write_text(json.dumps(payload))
    with pytest.raises(LanguagePublicationError) as caught:
        read_language_export(tmp_path)
    assert str(caught.value) == message


class _RecordingHub:
    def __init__(self, *, fail: bool = False) -> None:
        self.calls: list[object] = []
        self.revision = "before"
        self.files: tuple[RemoteFile, ...] = ()
        self.fail = fail

    def repo_revision(self, repo_id: str) -> str:
        self.calls.append(("revision", repo_id))
        return self.revision

    def upload(self, plan: UploadPlan, *, parent_revision: str | None = None) -> None:
        self.calls.append(("upload", plan, parent_revision))
        if self.fail:
            raise RuntimeError("synthetic upload failure")
        self.revision = "after"
        self.files = tuple(
            RemoteFile(item.relative_path, item.size_bytes, item.sha256) for item in plan.files
        )

    def paths_info(
        self, repo_id: str, revision: str, paths: Sequence[str]
    ) -> tuple[RemoteFile, ...]:
        self.calls.append(("paths", repo_id, revision, tuple(paths)))
        return self.files

    def dataset_configs(self, repo_id: str, revision: str) -> tuple[str, ...]:
        self.calls.append(("configs", repo_id, revision))
        return ("language-v1",)


@pytest.mark.parametrize("mode", ["planned", "verified", "ambiguous", "drifted"])
def test_publication_records_exact_outcome_and_forwards_hub_arguments(
    completed_export: LanguageExport, tmp_path: Path, mode: str
) -> None:
    plan = build_language_upload_plan(
        completed_export, "owner/dataset", confirm_repo="owner/dataset"
    )
    hub = _RecordingHub(fail=mode == "ambiguous")
    state = tmp_path / "publication.json"
    outcome = publish_language_export(
        plan,
        hub,
        baseline_revision="old" if mode == "drifted" else "before",
        apply=mode != "planned",
        state_path=state,
    )
    issues = {
        "planned": (),
        "verified": (),
        "ambiguous": (
            "the upload did not report success; verify the repository "
            "before attempting to publish again",
            "upload error: RuntimeError: synthetic upload failure",
        ),
        "drifted": (
            "repository moved from old to before; "
            "re-plan against the current revision before publishing",
        ),
    }[mode]
    expected = PublicationOutcome(
        PublishStatus(mode),
        plan.repo_id,
        plan.identity_sha256,
        "after" if mode == "verified" else "before",
        tuple(item.relative_path for item in plan.files) if mode == "verified" else (),
        issues,
    )
    assert outcome == expected
    assert outcome.is_verified is (mode == "verified")
    calls: list[object] = [("revision", plan.repo_id)]
    if mode in {"verified", "ambiguous"}:
        calls.append(("upload", plan, "before"))
    if mode == "verified":
        calls.extend(
            [
                ("revision", plan.repo_id),
                ("paths", plan.repo_id, "after", tuple(item.relative_path for item in plan.files)),
                ("configs", plan.repo_id, "after"),
            ]
        )
    assert hub.calls == calls
    assert read_language_publication_state(state) == (None if mode == "planned" else expected)


def test_verification_keeps_all_file_issues_and_sorts_verified_paths(tmp_path: Path) -> None:
    names = [f"language-v1/{name}" for name in ("z", "missing", "size", "checksum", "a")]
    plan = UploadPlan(
        "owner/dataset",
        str(tmp_path),
        tuple(UploadItem(name, 10, "a" * 64) for name in names),
        "b" * 64,
    )

    class IncompleteHub(_RecordingHub):
        def dataset_configs(self, repo_id: str, revision: str) -> tuple[str, ...]:
            super().dataset_configs(repo_id, revision)
            return ("default",)

    hub = IncompleteHub()
    hub.files = (
        RemoteFile(names[0], 10, "a" * 64),
        RemoteFile(names[2], 11, "a" * 64),
        RemoteFile(names[3], 10, "b" * 64),
        RemoteFile(names[4], 10, "a" * 64),
    )
    assert verify_language_publication(plan, hub, revision="pinned") == (
        (names[4], names[0]),
        (
            f"remote file is missing: {names[1]}",
            f"remote size differs for {names[2]}",
            f"remote checksum differs for {names[3]}",
            "the Dataset Viewer does not expose the language-v1 configuration",
        ),
    )
    assert hub.calls == [
        ("paths", plan.repo_id, "pinned", tuple(names)),
        ("configs", plan.repo_id, "pinned"),
    ]
