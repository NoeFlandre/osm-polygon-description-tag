"""Additive language-v1 export, allowlisting, and guarded publication."""

import hashlib
import json
from collections.abc import Sequence
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace

import pyarrow as pa
import pyarrow.parquet as pq
import pytest
from shapely.geometry import Polygon

from osm_polygon_description_tag.dataset.languages.annotations import ANNOTATION_SCHEMA
from osm_polygon_description_tag.dataset.languages.checkpoint import (
    WorkerBusyError,
    exclusive_worker_lock,
    part_name_for_offset,
    shard_paths,
)
from osm_polygon_description_tag.dataset.languages.models import (
    CASCADE_DETECTOR_NAME,
    GLOTLID_MODEL_REPOSITORY,
    GLOTLID_MODEL_REVISION,
    LanguagePolicy,
    LanguageResult,
    LanguageStatus,
    cascade_model_identity,
)
from osm_polygon_description_tag.dataset.languages.snapshot import (
    SnapshotManifest,
    prepare_snapshot,
)
from osm_polygon_description_tag.dataset.languages.worker import process_shard
from osm_polygon_description_tag.publication import language as language_module
from osm_polygon_description_tag.publication.language import (
    LANGUAGE_CONFIG_NAME,
    LANGUAGE_DATA_PREFIX,
    LANGUAGE_MANIFEST_PATH,
    LANGUAGE_STATS_PATH,
    LanguageExport,
    LanguagePublicationError,
    _export_name,
    build_language_upload_plan,
    export_language_annotations,
    language_config_yaml,
    read_language_export,
    render_language_card_section,
)
from osm_polygon_description_tag.publication.language_upload import (
    PUBLICATION_STATE_FILENAME,
    PublicationOutcome,
    PublishStatus,
    RemoteFile,
    publish_language_export,
    read_publication_state,
    verify_language_publication,
)
from osm_polygon_description_tag.publication.models import UploadItem, UploadPlan
from osm_polygon_description_tag.storage import write_geoparquet
from tests.conftest import make_record_dict
from tests.helpers.messages import exactly
from tests.helpers.sentences import fake_splitter

REPO = "NoeFlandre/osm-polygon-description-tag"
SHARD = "region.parquet"
OTHER = "other.parquet"


def _detector(text: str) -> LanguageResult:
    if text.startswith("Le "):
        return LanguageResult("fra", 0.95, 0.05, 0.9, LanguageStatus.DETECTED, "detected")
    if text.startswith("?"):
        return LanguageResult(None, 0.4, 0.3, 0.1, LanguageStatus.UNCERTAIN, "low_confidence")
    return LanguageResult("eng", 0.9, 0.1, 0.8, LanguageStatus.DETECTED, "detected")


def _tags(index: int) -> dict[str, str]:
    tags = {"description": f"A synthetic description {index}"}
    if index % 2 == 0:
        tags["description:fr"] = f"Le mur {index}"
    if index % 3 == 0:
        tags["description:zz"] = "?"
    return tags


def _write_shard(path: Path, count: int, *, start: int = 0) -> None:
    records = (
        make_record_dict(Polygon([(0, 0), (0, 1), (1, 1), (1, 0)]), _tags(index), osm_id=index + 1)
        for index in range(start, start + count)
    )
    path.parent.mkdir(parents=True, exist_ok=True)
    write_geoparquet(records, path, batch_size=3)


@pytest.fixture
def run_dir(tmp_path: Path) -> tuple[Path, Path, SnapshotManifest]:
    source = tmp_path / "source"
    _write_shard(source / SHARD, 6)
    run = tmp_path / "run"
    snapshot = prepare_snapshot(source, run, code_fingerprint="a" * 64, lock_fingerprint="b" * 64)
    return source, run, snapshot


def _process(source: Path, run: Path, snapshot: SnapshotManifest, shard: str = SHARD) -> None:
    process_shard(
        run,
        source,
        shard,
        detector=_detector,
        splitter=fake_splitter(),
        snapshot=snapshot,
        batch_size=3,
    )


@pytest.fixture
def export(run_dir: tuple[Path, Path, SnapshotManifest], tmp_path: Path) -> LanguageExport:
    source, run, snapshot = run_dir
    _process(source, run, snapshot)
    return export_language_annotations(run, tmp_path / "export")


class _FakeHub:
    """A faked Hub that records calls and never reaches the network."""

    def __init__(
        self,
        *,
        revisions: list[str] | None = None,
        configs: tuple[str, ...] = (LANGUAGE_CONFIG_NAME,),
        fail_upload: bool = False,
        corrupt: str | None = None,
        missing: str | None = None,
    ) -> None:
        self._revisions = revisions or ["rev-1", "rev-1"]
        self._configs = configs
        self._fail_upload = fail_upload
        self._corrupt = corrupt
        self._missing = missing
        self.uploads: list[UploadPlan] = []
        self.stored: dict[str, RemoteFile] = {}

    def repo_revision(self, repo_id: str) -> str:
        return self._revisions.pop(0) if len(self._revisions) > 1 else self._revisions[0]

    def upload(self, plan: UploadPlan, *, parent_revision: str | None = None) -> None:
        if self._fail_upload:
            raise RuntimeError("network died mid-upload")
        self.uploads.append(plan)
        for item in plan.files:
            size = item.size_bytes + (1 if item.relative_path == self._corrupt else 0)
            self.stored[item.relative_path] = RemoteFile(item.relative_path, size, item.sha256)

    def paths_info(
        self, repo_id: str, revision: str, paths: Sequence[str]
    ) -> tuple[RemoteFile, ...]:
        return tuple(
            self.stored[path] for path in paths if path in self.stored and path != self._missing
        )

    def dataset_configs(self, repo_id: str, revision: str) -> tuple[str, ...]:
        return self._configs


def test_interrupted_upload_has_durable_intent_and_is_not_repeated(
    export: LanguageExport, tmp_path: Path
) -> None:
    plan = build_language_upload_plan(export, repo_id=REPO, confirm_repo=REPO)
    state = tmp_path / "publication.json"
    observed: list[PublicationOutcome | None] = []

    class InterruptedHub(_FakeHub):
        def upload(self, plan: UploadPlan, *, parent_revision: str | None = None) -> None:
            observed.append(read_publication_state(state))
            super().upload(plan, parent_revision=parent_revision)
            raise KeyboardInterrupt

    hub = InterruptedHub()
    with pytest.raises(KeyboardInterrupt):
        publish_language_export(plan, hub, baseline_revision="rev-1", apply=True, state_path=state)

    assert observed[0] is not None
    assert observed[0].status is PublishStatus.AMBIGUOUS
    assert observed[0].plan_identity == plan.identity_sha256

    outcome = publish_language_export(
        plan, hub, baseline_revision="rev-1", apply=True, state_path=state
    )

    assert outcome.is_verified
    assert hub.uploads == [plan]


def test_library_publication_uses_durable_state_without_an_explicit_state_path(
    export: LanguageExport,
) -> None:
    plan = build_language_upload_plan(export, REPO, confirm_repo=REPO)
    hub = _FakeHub(fail_upload=True)

    outcome = publish_language_export(plan, hub, baseline_revision="rev-1", apply=True)

    assert read_publication_state(export.export_root / PUBLICATION_STATE_FILENAME) == outcome


def test_concurrent_publication_is_refused_before_contacting_the_hub(
    export: LanguageExport,
) -> None:
    plan = build_language_upload_plan(export, REPO, confirm_repo=REPO)
    hub = _FakeHub()

    with exclusive_worker_lock(export.export_root / "language-v1"), pytest.raises(WorkerBusyError):
        publish_language_export(plan, hub, baseline_revision="rev-1", apply=True)

    assert hub.uploads == []


def test_export_cannot_replace_files_while_publication_owns_the_namespace(
    export: LanguageExport, run_dir: tuple[Path, Path, SnapshotManifest]
) -> None:
    _, run, _ = run_dir
    before = (export.export_root / LANGUAGE_STATS_PATH).read_bytes()

    with exclusive_worker_lock(export.export_root / "language-v1"), pytest.raises(WorkerBusyError):
        export_language_annotations(run, export.export_root)

    assert (export.export_root / LANGUAGE_STATS_PATH).read_bytes() == before


def test_a_stale_upload_plan_is_rejected_before_network_writes(export: LanguageExport) -> None:
    plan = build_language_upload_plan(export, REPO, confirm_repo=REPO)
    path = export.export_root / export.files[0]
    path.write_bytes(path.read_bytes() + b"changed")
    hub = _FakeHub()

    with pytest.raises(
        LanguagePublicationError,
        match=exactly("export files changed after the completed export manifest"),
    ):
        publish_language_export(plan, hub, baseline_revision="rev-1", apply=True)

    assert hub.uploads == []


def test_export_refuses_an_incomplete_run(
    run_dir: tuple[Path, Path, SnapshotManifest], tmp_path: Path
) -> None:
    _, run, _ = run_dir

    with pytest.raises(LanguagePublicationError, match="run is not publishable"):
        export_language_annotations(run, tmp_path / "export")


def test_incomplete_run_reports_the_first_shard_problem_exactly(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    report = SimpleNamespace(
        is_complete=False,
        issues=(),
        shards=(SimpleNamespace(issues=("missing committed part",)),),
    )
    monkeypatch.setattr(language_module, "validate_run", lambda _run: report)

    with pytest.raises(
        LanguagePublicationError,
        match=exactly("run is not publishable: missing committed part"),
    ):
        language_module._require_complete_run(tmp_path / "run")


def test_incomplete_run_without_details_uses_the_stable_fallback_exactly(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    report = SimpleNamespace(is_complete=False, issues=(), shards=())
    monkeypatch.setattr(language_module, "validate_run", lambda _run: report)

    with pytest.raises(
        LanguagePublicationError,
        match=exactly("run is not publishable: not every shard is complete"),
    ):
        language_module._require_complete_run(tmp_path / "run")


def test_export_records_its_in_progress_manifest_before_writing_a_shard(
    run_dir: tuple[Path, Path, SnapshotManifest],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source, run, snapshot = run_dir
    _process(source, run, snapshot)
    export_root = tmp_path / "export"

    def stop_before_the_first_shard(*_args: object, **_kwargs: object) -> None:
        raise OSError("stop after manifest")

    monkeypatch.setattr(language_module, "_write_shard_export", stop_before_the_first_shard)

    with pytest.raises(OSError, match=exactly("stop after manifest")):
        export_language_annotations(run, export_root)

    assert json.loads((export_root / LANGUAGE_MANIFEST_PATH).read_text()) == {
        "schema_version": 1,
        "status": "in_progress",
        "snapshot_id": snapshot.snapshot_id,
    }


def test_export_preserves_a_non_default_detector_identity(tmp_path: Path) -> None:
    source = tmp_path / "source"
    _write_shard(source / SHARD, 2)
    run = tmp_path / "run"
    snapshot = prepare_snapshot(
        source,
        run,
        code_fingerprint="a" * 64,
        lock_fingerprint="b" * 64,
        model_identity=cascade_model_identity(LanguagePolicy()),
    )
    _process(source, run, snapshot)

    export = export_language_annotations(run, tmp_path / "export")

    assert export.detector_name == CASCADE_DETECTOR_NAME
    assert read_language_export(export.export_root).detector_name == CASCADE_DETECTOR_NAME


def test_export_refuses_a_run_with_a_corrupt_part(
    run_dir: tuple[Path, Path, SnapshotManifest], tmp_path: Path
) -> None:
    source, run, snapshot = run_dir
    _process(source, run, snapshot)
    shard_paths(run, SHARD).part(part_name_for_offset(0)).write_bytes(b"corrupt")

    with pytest.raises(LanguagePublicationError, match="run is not publishable"):
        export_language_annotations(run, tmp_path / "export")


def test_export_writes_one_additive_file_per_shard(export: LanguageExport) -> None:
    assert export.config_name == LANGUAGE_CONFIG_NAME
    assert export.files == (f"{LANGUAGE_DATA_PREFIX}/region.parquet",)
    exported = export.export_root / export.files[0]
    assert exported.is_file()
    assert pq.read_table(exported).schema == ANNOTATION_SCHEMA
    assert (export.export_root / LANGUAGE_STATS_PATH).is_file()


def test_export_only_writes_under_the_additive_prefix(export: LanguageExport) -> None:
    written = sorted(
        path.relative_to(export.export_root).as_posix()
        for path in export.export_root.rglob("*")
        if path.is_file()
    )

    assert written
    assert all(path.startswith("language-v1/") for path in written)


@pytest.mark.parametrize("target", ["data", "stats"])
def test_export_drift_is_refused_before_a_new_upload_plan_is_built(
    export: LanguageExport, target: str
) -> None:
    relative = export.files[0] if target == "data" else LANGUAGE_STATS_PATH
    path = export.export_root / relative
    path.write_bytes(path.read_bytes() + b"\n")

    with pytest.raises(LanguagePublicationError, match="export.*changed"):
        build_language_upload_plan(export, REPO, confirm_repo=REPO)

    with pytest.raises(LanguagePublicationError, match="export.*changed"):
        read_language_export(export.export_root)


@pytest.mark.parametrize("content", [None, b"{incomplete", b"{}"])
def test_an_export_requires_its_completed_manifest(
    export: LanguageExport, content: bytes | None
) -> None:
    path = export.export_root / LANGUAGE_MANIFEST_PATH
    if content is None:
        path.unlink()
    else:
        path.write_bytes(content)

    with pytest.raises(LanguagePublicationError):
        read_language_export(export.export_root)


def test_parent_directory_symlinks_cannot_redirect_uploads_outside_the_export(
    export: LanguageExport, tmp_path: Path
) -> None:
    data = export.export_root / LANGUAGE_DATA_PREFIX
    external = tmp_path / "external-data"
    data.rename(external)
    data.symlink_to(external, target_is_directory=True)

    with pytest.raises(LanguagePublicationError, match="unsafe"):
        build_language_upload_plan(export, REPO, confirm_repo=REPO)


def test_export_refuses_unsafe_output_parents_before_writing_any_file(
    export: LanguageExport, run_dir: tuple[Path, Path, SnapshotManifest], tmp_path: Path
) -> None:
    _, run, _ = run_dir
    data = export.export_root / LANGUAGE_DATA_PREFIX
    external = tmp_path / "external-data"
    data.rename(external)
    data.symlink_to(external, target_is_directory=True)
    protected = external / Path(export.files[0]).name
    protected.write_bytes(b"leave outside files alone")

    with pytest.raises(LanguagePublicationError, match="unsafe"):
        export_language_annotations(run, export.export_root)

    assert protected.read_bytes() == b"leave outside files alone"


def test_export_counts_come_from_the_exported_rows(export: LanguageExport) -> None:
    stats = export.stats
    rows = pq.read_table(export.export_root / export.files[0]).to_pylist()

    assert stats.annotation_count == len(rows)
    assert stats.object_count == 6
    assert stats.base_description_count == 6
    assert stats.localized_description_count == len(rows) - 6
    assert stats.detected_count + stats.uncertain_count + stats.non_linguistic_count == len(rows)
    assert stats.distinct_language_count == 2
    assert dict(stats.top_languages)["eng"] == 6
    assert stats.uncertain_count == sum(1 for row in rows if row["status"] == "uncertain")


def test_export_stats_are_written_as_json(export: LanguageExport) -> None:
    payload = json.loads((export.export_root / LANGUAGE_STATS_PATH).read_text(encoding="utf-8"))

    assert payload["config_name"] == LANGUAGE_CONFIG_NAME
    assert payload["stats"]["annotation_count"] == export.stats.annotation_count
    assert payload["library_version"] == "2.2.0"
    assert payload["snapshot_id"] == export.snapshot_id


def test_missing_completed_manifest_error_is_not_silenced(export: LanguageExport) -> None:
    (export.export_root / LANGUAGE_MANIFEST_PATH).unlink()

    with pytest.raises(LanguagePublicationError) as caught:
        language_module._validate_export_seal(export)

    assert str(caught.value).startswith("cannot read completed export manifest:")


def test_export_seal_rejection_message_is_exact(export: LanguageExport) -> None:
    path = export.export_root / LANGUAGE_STATS_PATH
    path.write_bytes(path.read_bytes() + b"\n")

    with pytest.raises(LanguagePublicationError) as caught:
        language_module._validate_export_seal(export)

    assert str(caught.value) == "export files changed after the completed export manifest"


def test_export_covers_every_shard(tmp_path: Path) -> None:
    source = tmp_path / "source"
    _write_shard(source / SHARD, 6)
    _write_shard(source / OTHER, 3, start=100)
    run = tmp_path / "run"
    snapshot = prepare_snapshot(source, run, code_fingerprint="a" * 64, lock_fingerprint="b" * 64)
    _process(source, run, snapshot, SHARD)
    _process(source, run, snapshot, OTHER)

    export = export_language_annotations(run, tmp_path / "export")

    assert export.files == (
        f"{LANGUAGE_DATA_PREFIX}/other.parquet",
        f"{LANGUAGE_DATA_PREFIX}/region.parquet",
    )
    assert export.stats.object_count == 9


def test_the_plan_requires_an_exact_repository_confirmation(export: LanguageExport) -> None:
    with pytest.raises(LanguagePublicationError, match="does not match"):
        build_language_upload_plan(export, REPO, confirm_repo="someone/else")


def test_the_plan_contains_only_additive_paths(export: LanguageExport) -> None:
    plan = build_language_upload_plan(export, REPO, confirm_repo=REPO)

    assert plan.repo_id == REPO
    assert [item.relative_path for item in plan.files] == [
        f"{LANGUAGE_DATA_PREFIX}/region.parquet",
        LANGUAGE_MANIFEST_PATH,
        LANGUAGE_STATS_PATH,
    ]
    assert all(item.relative_path.startswith("language-v1/") for item in plan.files)
    assert len(plan.identity_sha256) == 64


def test_export_name_does_not_strip_valid_letter_characters() -> None:
    assert _export_name("X-region-X.parquet") == "x-region-x.parquet"


def test_the_card_section_names_the_primary_model_and_avoids_duplicate_licensing(
    export: LanguageExport,
) -> None:
    section = render_language_card_section(export)

    assert "**Models and provenance.**" in section
    assert f"The primary detector is Lingua {export.library_version}." in section
    assert "**Licensing.**" not in section


def test_completed_reexport_changes_identity_and_invalidates_the_prior_plan(
    export: LanguageExport, tmp_path: Path
) -> None:
    first = build_language_upload_plan(export, REPO, confirm_repo=REPO)
    source = tmp_path / "other-source"
    _write_shard(source / SHARD, 2, start=100)
    run = tmp_path / "other-run"
    snapshot = prepare_snapshot(source, run, code_fingerprint="a" * 64, lock_fingerprint="b" * 64)
    _process(source, run, snapshot)
    updated = export_language_annotations(run, export.export_root)
    second = build_language_upload_plan(updated, REPO, confirm_repo=REPO)

    assert first.identity_sha256 != second.identity_sha256
    hub = _FakeHub()
    with pytest.raises(
        LanguagePublicationError,
        match=exactly("export changed after the upload plan was created"),
    ):
        publish_language_export(first, hub, baseline_revision="rev-1", apply=True)
    assert hub.uploads == []


def test_the_plan_rejects_a_missing_export_file(export: LanguageExport) -> None:
    (export.export_root / export.files[0]).unlink()

    with pytest.raises(LanguagePublicationError, match="export file is missing"):
        build_language_upload_plan(export, REPO, confirm_repo=REPO)


def test_the_plan_rejects_a_path_outside_the_namespace(export: LanguageExport) -> None:
    from dataclasses import replace

    foreign = replace(export, files=("data/000.parquet",))

    with pytest.raises(LanguagePublicationError, match="may only contain language-v1/ paths"):
        build_language_upload_plan(foreign, REPO, confirm_repo=REPO)


def test_the_card_section_reports_validated_counts_and_no_accuracy_claim(
    export: LanguageExport,
) -> None:
    section = render_language_card_section(export)

    assert f"| Annotations | {export.stats.annotation_count} |" in section
    # Object, base and localized counts are deliberately absent: the card
    # already reports them under "Description coverage", and repeating them
    # here is what made this section read as a bolted-on appendix.
    assert "Distinct OSM objects" not in section
    assert "Base `description` values" not in section
    assert export.snapshot_id in section
    assert export.model_config_fingerprint in section
    prose = " ".join(section.replace("**", "").split())
    assert "raw detector scores, not calibrated probabilities" in prose
    assert "must not be read as confidence percentages" in prose
    assert (
        "No accuracy has been measured because this dataset has no ground-truth labels" in section
    )
    assert "`language_code` is null for uncertain or non-linguistic values" in prose
    assert "opaque" in section
    assert "mixed_text" in section
    assert "may be misclassified as a supported language" in prose
    assert "mixed_text" in prose


def test_the_card_section_identifies_the_cascade_fallback(export: LanguageExport) -> None:
    cascade = replace(export, detector_name=CASCADE_DETECTOR_NAME)

    section = render_language_card_section(cascade)

    assert f"pipeline `{CASCADE_DETECTOR_NAME}`" in section
    assert f"pinned GlotLID v3 model (`{GLOTLID_MODEL_REPOSITORY}`" in section
    assert GLOTLID_MODEL_REVISION in section
    assert "used as a fallback" in section


def test_the_card_section_never_claims_a_measured_score(export: LanguageExport) -> None:
    prose = " ".join(render_language_card_section(export).replace("**", "").split()).lower()

    for claim in ("accuracy of", "f1 score", "% accurate", "precision of", "recall of"):
        assert claim not in prose
    assert "no accuracy has been measured" in prose


def test_the_additive_config_entry_points_at_the_namespace() -> None:
    yaml = language_config_yaml()

    assert f"config_name: {LANGUAGE_CONFIG_NAME}" in yaml
    assert f"path: {LANGUAGE_DATA_PREFIX}/*.parquet" in yaml
    assert "split: train" in yaml


def test_publishing_without_the_apply_gate_uploads_nothing(export: LanguageExport) -> None:
    plan = build_language_upload_plan(export, REPO, confirm_repo=REPO)
    hub = _FakeHub()

    outcome = publish_language_export(plan, hub, baseline_revision="rev-1")

    assert outcome.status is PublishStatus.PLANNED
    assert not outcome.is_verified
    assert hub.uploads == []


def test_dry_run_drift_does_not_create_publication_state(export: LanguageExport) -> None:
    plan = build_language_upload_plan(export, REPO, confirm_repo=REPO)
    state = export.export_root / PUBLICATION_STATE_FILENAME

    outcome = publish_language_export(plan, _FakeHub(revisions=["new"]), baseline_revision="old")

    assert outcome.status is PublishStatus.DRIFTED
    assert not state.exists()


def test_failed_reexport_invalidates_the_old_completion_manifest(
    export: LanguageExport,
    run_dir: tuple[Path, Path, SnapshotManifest],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from osm_polygon_description_tag.publication import language

    original = language._write_shard_export

    def fail_write(*args: object, **kwargs: object) -> None:
        raise OSError("synthetic interrupted export")

    monkeypatch.setattr(language, "_write_shard_export", fail_write)
    with pytest.raises(OSError, match="synthetic interrupted export"):
        export_language_annotations(run_dir[1], export.export_root)
    with pytest.raises(LanguagePublicationError, match="completed export manifest"):
        read_language_export(export.export_root)

    monkeypatch.setattr(language, "_write_shard_export", original)
    assert export_language_annotations(run_dir[1], export.export_root) == export
    assert read_language_export(export.export_root) == export


def test_a_moved_repository_is_refused_rather_than_overwritten(export: LanguageExport) -> None:
    plan = build_language_upload_plan(export, REPO, confirm_repo=REPO)
    hub = _FakeHub(revisions=["rev-9"])

    outcome = publish_language_export(plan, hub, baseline_revision="rev-1", apply=True)

    assert outcome.status is PublishStatus.DRIFTED
    assert hub.uploads == []
    assert any("repository moved from rev-1 to rev-9" in issue for issue in outcome.issues)


def test_a_successful_publication_is_verified_against_the_hub(export: LanguageExport) -> None:
    plan = build_language_upload_plan(export, REPO, confirm_repo=REPO)
    hub = _FakeHub()

    outcome = publish_language_export(plan, hub, baseline_revision="rev-1", apply=True)

    assert outcome.status is PublishStatus.VERIFIED
    assert outcome.is_verified
    assert outcome.issues == ()
    assert len(hub.uploads) == 1
    assert outcome.verified_files == tuple(sorted(item.relative_path for item in plan.files))


def test_replanning_after_drift_can_publish_against_the_new_revision(
    export: LanguageExport,
) -> None:
    plan = build_language_upload_plan(export, REPO, confirm_repo=REPO)
    hub = _FakeHub(revisions=["rev-9"])
    first = publish_language_export(plan, hub, baseline_revision="rev-1", apply=True)
    assert first.status is PublishStatus.DRIFTED

    second = publish_language_export(plan, hub, baseline_revision="rev-9", apply=True)

    assert second.status is PublishStatus.VERIFIED
    assert len(hub.uploads) == 1


def test_repeating_an_outdated_baseline_still_refuses_upload(export: LanguageExport) -> None:
    plan = build_language_upload_plan(export, REPO, confirm_repo=REPO)
    hub = _FakeHub(revisions=["rev-9"])
    publish_language_export(plan, hub, baseline_revision="rev-1", apply=True)

    second = publish_language_export(plan, hub, baseline_revision="rev-1", apply=True)

    assert second.status is PublishStatus.DRIFTED
    assert hub.uploads == []


def test_a_failed_upload_is_ambiguous_and_recorded(export: LanguageExport, tmp_path: Path) -> None:
    plan = build_language_upload_plan(export, REPO, confirm_repo=REPO)
    state = tmp_path / "state.json"

    outcome = publish_language_export(
        plan, _FakeHub(fail_upload=True), baseline_revision="rev-1", apply=True, state_path=state
    )

    assert outcome.status is PublishStatus.AMBIGUOUS
    assert any("verify the repository" in issue for issue in outcome.issues)
    assert read_publication_state(state) == outcome


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

    assert read_publication_state(state) == outcome
    assert read_publication_state(tmp_path / "absent.json") is None


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
        read_publication_state(state)


def test_an_unreadable_publication_state_is_reported(tmp_path: Path) -> None:
    state = tmp_path / "state.json"
    state.write_text("{not-json", encoding="utf-8")

    with pytest.raises(LanguagePublicationError, match="cannot read publication state"):
        read_publication_state(state)


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
    from dataclasses import replace

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


# Export byte contracts and exact publication protocol boundaries.


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
    assert read_publication_state(state) == (None if mode == "planned" else expected)


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
