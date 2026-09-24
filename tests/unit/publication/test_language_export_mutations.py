"""Export byte contracts and exact publication protocol boundaries."""

import hashlib
import json
from collections.abc import Sequence
from pathlib import Path

import pyarrow as pa
import pyarrow.parquet as pq
import pytest
from shapely.geometry import Polygon

from osm_polygon_description_tag.dataset.languages.checkpoint import (
    part_name_for_offset,
    shard_paths,
)
from osm_polygon_description_tag.dataset.languages.models import LanguageResult, LanguageStatus
from osm_polygon_description_tag.dataset.languages.snapshot import prepare_snapshot
from osm_polygon_description_tag.dataset.languages.worker import process_shard
from osm_polygon_description_tag.publication import language as export_module
from osm_polygon_description_tag.publication.language import (
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
from osm_polygon_description_tag.storage import write_geoparquet
from tests.conftest import make_record_dict
from tests.helpers.sentences import fake_splitter

SHARD = "region.parquet"


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
    stats = export_module._StatsAccumulator()

    export_module._write_shard_export(target, paths, (part.name,), stats)

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
        export_module._identified_plan(repo, root, files, "## card\n")
        for repo, root, files in variants
    ]

    assert len({plan.identity_sha256 for plan in plans}) == len(variants)
    # The card is held constant here, so any difference comes from the repo,
    # the root or the files -- which is what this test is about.
    assert (
        export_module._identified_plan(*variants[0], "## other\n").identity_sha256
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
