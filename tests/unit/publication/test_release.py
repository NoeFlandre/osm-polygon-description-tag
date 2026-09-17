"""Contract tests for the deterministic metadata release path.

The release path must validate the complete published inventory, recompute the
card and report, publish exactly two documents plus the required visual
assets, verify the remote revision, and stay byte-stable across runs.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from osm_polygon_description_tag.publication import (
    REPO_ID,
    PublicationError,
    ReleaseReport,
    UploadItem,
    release_metadata,
    validate_published_inventory,
)
from osm_polygon_description_tag.publication import release as release_module
from osm_polygon_description_tag.publication.language_card import (
    LANGUAGE_CARD_SECTION_END,
    LANGUAGE_CARD_SECTION_START,
)
from osm_polygon_description_tag.publication.verification import HubVerificationError
from osm_polygon_description_tag.runtime.resources import dataset_card_template
from tests.helpers.dataset import write_reporting_fixture


class _RecordingVerifier:
    """Stand-in Hub verifier returning a fixed revision."""

    def __init__(self, revision: str = "deadbeef") -> None:
        self.revision = revision
        self.calls: list[tuple[str, tuple[UploadItem, ...]]] = []
        self.inventory_calls: list[tuple[str, tuple[UploadItem, ...], str | None]] = []
        self.matching_calls: list[tuple[str, tuple[UploadItem, ...]]] = []
        self.remote_metadata_revision: str | None = None

    def __call__(self, repo_id: str, files: tuple[UploadItem, ...]) -> str:
        self.calls.append((repo_id, files))
        return self.revision

    def verify_inventory(
        self,
        repo_id: str,
        files: tuple[UploadItem, ...],
        *,
        revision: str | None = None,
    ) -> str:
        self.inventory_calls.append((repo_id, files, revision))
        return revision or "data-revision"

    def matching_revision(self, repo_id: str, files: tuple[UploadItem, ...]) -> str | None:
        self.matching_calls.append((repo_id, files))
        return self.remote_metadata_revision


@pytest.fixture
def workspace(tmp_path: Path) -> Path:
    data_root = tmp_path / "generated"
    data_root.mkdir()
    write_reporting_fixture(data_root, tmp_path / "raw")
    return data_root


def _release(data_root: Path, **kwargs: object) -> object:
    return release_metadata(
        data_root,
        dataset_card_template(),
        confirm_repo=kwargs.pop("confirm_repo", REPO_ID),  # type: ignore[arg-type]
        apply=bool(kwargs.pop("apply", False)),
        **kwargs,  # type: ignore[arg-type]
    )


_PRE_REGRESSION_CARD = Path(__file__).parents[2] / "fixtures" / "hf_description_card_7a9c678.md"
_CURRENT_5CAD_STATS_BLOCK = (
    "<!-- GENERATED:STATS:START -->\n"
    "<!-- stats_sha256: e3f23671bb7eaa3a2a15a07629b96730f424ca195e21b8e280fc52b5f303d449 -->\n"
    "<!-- stats_schema_version: 7 -->\n"
    "\n"
    "## Polygon surface and geometry\n"
    "\n"
    "Computed deterministically from the complete published polygon table: "
    "all 906,631 rows across 386 Parquet files, using only the dataset's area_m2, "
    "bbox, and geometry columns. No sampling, truncation, external lookup, or "
    "raw-PBF recomputation is used.\n"
    "\n"
    "| Metric | Value |\n"
    "| --- | ---: |\n"
    "| Polygons measured | 906,631 |\n"
    "| Surface area (total / mean) | 24,988,429.0 km² / 27.6 km² |\n"
    "| Smallest / largest area | 6.19e-05 m² / 3,477,486.0 km² |\n"
    "| Area p25 / median / p75 | 85.3 / 501.2 / 10,391.2 m² |\n"
    "| Dataset bounding box | [-179.1479°, -82.1117°] to [179.8992°, 83.6651°] |\n"
    "| Geometry totals (vertices / rings / holes / MultiPolygon parts) | "
    "45,960,475 / 1,029,446 / 70,335 / 959,111 |\n"
    "| Polygon / MultiPolygon rows | 0 / 906,631 |\n"
    "\n"
    "The complete machine-readable report is published in stats.json. These values "
    "are generated from the data only and are deterministic for unchanged published "
    "artifacts.\n"
    "<!-- GENERATED:STATS:END -->"
)


def _replace_stats_marker_block(readme: str, replacement: str) -> str:
    start_marker = "<!-- GENERATED:STATS:START -->"
    end_marker = "<!-- GENERATED:STATS:END -->"
    start = readme.index(start_marker)
    end = readme.index(end_marker, start) + len(end_marker)
    return readme[:start] + replacement + readme[end:]


def _without_stats_marker_block(readme: str) -> str:
    return _replace_stats_marker_block(readme, "")


def test_dry_run_computes_plan_without_uploading(workspace: Path) -> None:
    commands: list[list[str]] = []
    report = _release(workspace, runner=commands.append)

    assert report.repo_id == REPO_ID
    assert report.published is False
    assert report.revision is None
    assert report.validated_parquet_files == 2
    assert report.rows == 3
    assert commands == []
    assert [item.relative_path for item in report.files] == [
        "README.md",
        "stats.json",
        "assets/description_polygon_density.png",
        "assets/area_distribution.png",
        "assets/dataset-card-hero.png",
    ]


def test_apply_uploads_only_metadata_and_verifies(workspace: Path) -> None:
    commands: list[list[str]] = []
    verifier = _RecordingVerifier()

    report = _release(workspace, apply=True, runner=commands.append, verifier=verifier)

    assert report.published is True
    assert report.revision == "deadbeef"
    assert len(commands) == 1
    included = [
        command_argument
        for index, command_argument in enumerate(commands[0])
        if commands[0][index - 1] == "--include"
    ]
    assert included == [item.relative_path for item in report.files]
    assert not any(argument.startswith("data/") for argument in included)
    assert not any(argument.startswith("manifests/") for argument in included)
    assert verifier.calls == [(REPO_ID, report.files)]
    assert report.data_revision == "data-revision"
    assert verifier.inventory_calls[0][0] == REPO_ID
    assert verifier.inventory_calls[0][2] is None
    assert [item.relative_path for item in verifier.inventory_calls[0][1]] == [
        "data/region-a.parquet",
        "data/region-b.parquet",
        "manifests/region-a.manifest.json",
        "manifests/region-b.manifest.json",
    ]
    assert verifier.inventory_calls[1][2] == "deadbeef"


def test_apply_is_remote_idempotent_when_metadata_already_matches(workspace: Path) -> None:
    commands: list[list[str]] = []
    verifier = _RecordingVerifier()

    def observe_upload(_command: list[str]) -> None:
        verifier.remote_metadata_revision = "deadbeef"
        commands.append(_command)

    first = _release(workspace, apply=True, runner=observe_upload, verifier=verifier)
    second = _release(workspace, apply=True, runner=observe_upload, verifier=verifier)

    assert first.revision == second.revision == "deadbeef"
    assert len(commands) == 1
    assert len(verifier.matching_calls) == 2
    assert len(verifier.calls) == 1


def test_apply_preflights_remote_revision_before_computing_stats(
    workspace: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    events: list[str] = []
    verifier = _RecordingVerifier()
    original_generate = release_module.generate_dataset_docs

    def record_generate(*args: object, **kwargs: object) -> object:
        events.append("compute")
        return original_generate(*args, **kwargs)  # type: ignore[arg-type]

    def record_inventory(
        repo_id: str,
        files: tuple[UploadItem, ...],
        *,
        revision: str | None = None,
    ) -> str:
        events.append(f"inventory:{revision}")
        return _RecordingVerifier.verify_inventory(verifier, repo_id, files, revision=revision)

    monkeypatch.setattr(release_module, "generate_dataset_docs", record_generate)
    verifier.verify_inventory = record_inventory  # type: ignore[method-assign]

    _release(workspace, apply=True, runner=lambda _command: None, verifier=verifier)

    assert events == ["inventory:None", "compute", "inventory:deadbeef"]


def test_release_preserves_existing_language_card_content(workspace: Path) -> None:
    template = dataset_card_template().read_text(encoding="utf-8")
    language_config = (
        "- config_name: language-v1\n"
        "  data_files:\n"
        "  - split: train\n"
        "    path:\n"
        "    - language-v1/data/region.parquet\n"
    )
    other_config = (
        "- config_name: audit-v1\n"
        "  data_files:\n"
        "  - split: train\n"
        "    path: audit-v1/data/audit.parquet\n"
    )
    language_section = (
        f"{LANGUAGE_CARD_SECTION_START}\n"
        "## Language annotations (`language-v1`)\n\n"
        "Controlled language-v1 snapshot content.\n"
        f"{LANGUAGE_CARD_SECTION_END}\n"
    )
    other_section = (
        "<!-- GENERATED:AUDIT:START -->\n"
        "An unrelated generated section.\n"
        "<!-- GENERATED:AUDIT:END -->\n"
    )
    existing = template.replace("\n---\n\n", f"\n{language_config}{other_config}---\n\n", 1)
    existing = existing.replace(
        "\n## Terminology",
        f"\n{language_section}\n{other_section}<!-- preserved metadata -->\n\n## Terminology",
        1,
    )
    (workspace / "README.md").write_text(existing, encoding="utf-8")

    _release(workspace)

    updated = (workspace / "README.md").read_text(encoding="utf-8")
    assert language_config in updated
    assert other_config in updated
    assert language_section in updated
    assert other_section in updated
    assert "<!-- preserved metadata -->" in updated
    assert "| Polygon geometries |" in updated

    stats_start = "<!-- GENERATED:STATS:START -->"
    stats_end = "<!-- GENERATED:STATS:END -->"
    updated_without_stats = updated.replace(
        updated[updated.index(stats_start) : updated.index(stats_end) + len(stats_end)], ""
    )
    existing_without_stats = existing.replace(
        existing[existing.index(stats_start) : existing.index(stats_end) + len(stats_end)], ""
    )
    assert updated_without_stats == existing_without_stats


def test_read_remote_card_handles_optional_verifier_reader() -> None:
    class Reader:
        def read_file(self, repo_id: str, path: str, *, revision: str) -> str:
            assert (repo_id, path, revision) == (REPO_ID, "README.md", "revision")
            return "remote card"

    assert release_module._read_remote_card(_RecordingVerifier(), REPO_ID, "revision") is None
    assert release_module._read_remote_card(Reader(), REPO_ID, "revision") == "remote card"


def test_read_remote_card_rejects_incompatible_verifier_reader() -> None:
    class Reader:
        def read_file(self, repo_id: str, path: str) -> str:
            return f"{repo_id}:{path}"

    with pytest.raises(PublicationError, match="incompatible interface"):
        release_module._read_remote_card(Reader(), REPO_ID, "revision")


def test_read_remote_card_rejects_non_text_verifier_response() -> None:
    class Reader:
        def read_file(self, repo_id: str, path: str, *, revision: str) -> object:
            return {"repo_id": repo_id, "path": path, "revision": revision}

    with pytest.raises(PublicationError, match="non-text README"):
        release_module._read_remote_card(Reader(), REPO_ID, "revision")


def test_sync_remote_card_replaces_remote_card_and_cleans_temp(tmp_path: Path) -> None:
    target = tmp_path / "README.md"
    target.write_text("local card", encoding="utf-8")

    release_module._sync_remote_card(tmp_path, _RecordingVerifier(), REPO_ID, "revision")
    assert target.read_text(encoding="utf-8") == "local card"

    class Reader:
        def read_file(self, repo_id: str, path: str, *, revision: str) -> str:
            assert (repo_id, path, revision) == (REPO_ID, "README.md", "revision")
            return "remote card"

    release_module._sync_remote_card(tmp_path, Reader(), REPO_ID, "revision")

    assert target.read_text(encoding="utf-8") == "remote card"
    assert list(tmp_path.glob(".*.tmp")) == []


def test_apply_refuses_metadata_revision_mismatch(workspace: Path) -> None:
    commands: list[list[str]] = []

    class DivergentVerifier(_RecordingVerifier):
        def verify_inventory(
            self,
            repo_id: str,
            files: tuple[UploadItem, ...],
            *,
            revision: str | None = None,
        ) -> str:
            if revision is not None:
                return "different-revision"
            return super().verify_inventory(repo_id, files, revision=revision)

    with pytest.raises(PublicationError, match="different from the upload"):
        _release(
            workspace,
            apply=True,
            runner=commands.append,
            verifier=DivergentVerifier(),
        )

    assert len(commands) == 1


def test_release_restores_pre_regression_card_without_losing_content(
    workspace: Path,
) -> None:
    golden = _PRE_REGRESSION_CARD.read_text(encoding="utf-8")
    assert len(golden) == 27_804
    regressed = _replace_stats_marker_block(golden, _CURRENT_5CAD_STATS_BLOCK)
    (workspace / "README.md").write_text(regressed, encoding="utf-8")

    _release(workspace)

    updated = (workspace / "README.md").read_text(encoding="utf-8")
    assert _without_stats_marker_block(updated) == _without_stats_marker_block(golden)

    stats_start = updated.index("<!-- GENERATED:STATS:START -->")
    stats_end = updated.index("<!-- GENERATED:STATS:END -->", stats_start)
    generated_stats = updated[stats_start : stats_end + len("<!-- GENERATED:STATS:END -->")]
    for expected in (
        "<!-- stats_sha256: ",
        "<!-- stats_schema_version: ",
        "<!-- schema_version: ",
        "## Dataset at a glance",
        "| Regional/raw polygon rows | 3 |",
        "| Globally unique polygons | 3 |",
        "| Regional-overlap duplicate rows | 0 |",
        "| Manifest duplicate rows rejected | 0 |",
        "| Parquet files | 2 |",
        "## Description coverage",
        "| Localized descriptions | 3 |",
        "### Most common localized suffixes",
        "| " + chr(96) + "en" + chr(96) + " | 2 |",
        "| " + chr(96) + "pt-BR" + chr(96) + " | 1 |",
        "### Area distribution",
        "![Area distribution of description-tagged polygons](assets/area_distribution.png)",
        "**OSM object timestamps (UTC):**",
        (
            "Detailed machine-readable statistics, exact suffix frequencies, rejection counts, "
            "and per-file SHA-256 provenance are available in [`stats.json`](stats.json)."
        ),
        "## Polygon surface and geometry",
        "| Globally unique polygons measured | 3 |",
        "| Polygon / MultiPolygon rows | 2 / 1 |",
    ):
        assert expected in generated_stats

    preserved = _without_stats_marker_block(updated)
    for expected in (
        "- config_name: language-v1",
        "language-v1/data/zimbabwe-latest.parquet",
        "<!-- GENERATED:LANGUAGE_V1:START -->",
        "## Language annotations (" + chr(96) + "language-v1" + chr(96) + ")",
        "## Terminology",
        "## Schema",
        "## Load the data",
        "## Methodology",
        "## Limitations",
        "## License and attribution",
        "## Reproducibility",
        "## Citation",
        "https://github.com/NoeFlandre/osm-polygon-description-tag",
        "https://noeflandre-osm-polygon-description-tag-trackio.static.hf.space/",
        "https://noeflandre.github.io/osm-polygon-description-tag/slides/dataset/dataset.html",
        "https://opendatacommons.org/licenses/odbl/",
        "CITATION.cff",
    ):
        assert expected in preserved


def test_apply_requires_remote_inventory_verifier(workspace: Path) -> None:
    commands: list[list[str]] = []

    with pytest.raises(PublicationError, match="data/manifest inventory"):
        _release(workspace, apply=True, runner=commands.append, verifier=lambda *_args: "revision")

    assert commands == []


def test_apply_refuses_remote_inventory_mismatch(workspace: Path) -> None:
    commands: list[list[str]] = []

    class MismatchedVerifier(_RecordingVerifier):
        def verify_inventory(
            self,
            repo_id: str,
            files: tuple[UploadItem, ...],
            *,
            revision: str | None = None,
        ) -> str:
            raise HubVerificationError("remote data/manifest inventory mismatch")

    with pytest.raises(HubVerificationError, match="inventory mismatch"):
        _release(
            workspace,
            apply=True,
            runner=commands.append,
            verifier=MismatchedVerifier(),
        )

    assert commands == []


def test_apply_refuses_when_inventory_changes_after_upload(workspace: Path) -> None:
    commands: list[list[str]] = []

    class ChangingVerifier(_RecordingVerifier):
        def verify_inventory(
            self,
            repo_id: str,
            files: tuple[UploadItem, ...],
            *,
            revision: str | None = None,
        ) -> str:
            if revision is not None:
                raise HubVerificationError("remote data/manifest inventory changed")
            return super().verify_inventory(repo_id, files, revision=revision)

    with pytest.raises(HubVerificationError, match="inventory changed"):
        _release(
            workspace,
            apply=True,
            runner=commands.append,
            verifier=ChangingVerifier(),
        )

    assert len(commands) == 1


def test_second_run_is_a_byte_stable_no_op(workspace: Path) -> None:
    first = _release(workspace, runner=lambda command: None)
    readme = workspace / "README.md"
    stats = workspace / "stats.json"
    before = (readme.read_bytes(), stats.read_bytes(), readme.stat().st_mtime_ns)

    second = _release(workspace, runner=lambda command: None)

    assert second.plan_identity_sha256 == first.plan_identity_sha256
    assert (readme.read_bytes(), stats.read_bytes(), readme.stat().st_mtime_ns) == before


def test_wrong_repository_confirmation_is_refused(workspace: Path) -> None:
    with pytest.raises(PublicationError, match="repository confirmation"):
        _release(workspace, confirm_repo="someone-else/osm-polygon-description-tag")


def test_missing_inventory_is_refused(tmp_path: Path) -> None:
    with pytest.raises(PublicationError, match="published data directory missing"):
        validate_published_inventory(tmp_path)


def test_empty_inventory_is_refused(tmp_path: Path) -> None:
    (tmp_path / "data").mkdir()
    with pytest.raises(PublicationError, match="no published Parquet files"):
        validate_published_inventory(tmp_path)


def test_parquet_without_matching_manifest_is_refused(workspace: Path) -> None:
    (workspace / "manifests" / "region-a.manifest.json").unlink()
    with pytest.raises(PublicationError):
        validate_published_inventory(workspace)


def test_empty_remote_revision_is_refused(workspace: Path) -> None:
    verifier = _RecordingVerifier(revision="")

    with pytest.raises(PublicationError, match="empty revision"):
        _release(
            workspace,
            apply=True,
            runner=lambda command: None,
            verifier=verifier,
        )


def test_release_report_payload_serializes_file_evidence() -> None:
    report = ReleaseReport(
        repo_id=REPO_ID,
        data_root="generated",
        plan_identity_sha256="plan",
        files=(UploadItem("README.md", 4, "hash"),),
        validated_parquet_files=1,
        rows=2,
        published=True,
        revision="revision",
        data_revision="data-revision",
    )

    assert report.to_payload() == {
        "data_root": "generated",
        "data_revision": "data-revision",
        "files": [{"relative_path": "README.md", "sha256": "hash", "size_bytes": 4}],
        "plan_identity_sha256": "plan",
        "published": True,
        "repo_id": REPO_ID,
        "revision": "revision",
        "rows": 2,
        "validated_parquet_files": 1,
    }
