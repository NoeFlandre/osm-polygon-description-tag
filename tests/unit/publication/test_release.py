"""Contract tests for the deterministic metadata release path.

The release path must validate the complete published inventory, recompute the
card and report, publish exactly two documents plus the required visual
assets, verify the remote revision, and stay byte-stable across runs.
"""

from __future__ import annotations

import re
from pathlib import Path

import pyarrow as pa
import pyarrow.parquet as pq
import pytest

from osm_polygon_description_tag.dataset.geography import (
    H3_MAP_DESCRIPTION,
    H3_MAP_TITLE,
    normalize_map_prose,
)
from osm_polygon_description_tag.publication import (
    REPO_ID,
    PublicationError,
    ReleaseReport,
    UploadItem,
    UploadPlan,
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
from tests.helpers.messages import exactly


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
    "<!-- stats_schema_version: 8 -->\n"
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


_H3_MAP_BLOCK_PATTERN = re.compile(
    r"<!-- GENERATED:H3_MAP:START -->[^\n]*\n.*?<!-- GENERATED:H3_MAP:END -->\n",
    re.DOTALL,
)


def _without_generated_map_and_stats(readme: str) -> str:
    without_stats = _without_stats_marker_block(readme)
    return _H3_MAP_BLOCK_PATTERN.sub("", without_stats, count=1)


def test_dry_run_computes_plan_without_uploading(workspace: Path) -> None:
    commands: list[list[str]] = []
    report = _release(workspace, runner=commands.append)

    assert report.repo_id == REPO_ID
    assert report.published is False
    assert report.revision is None
    assert report.data_root == str(workspace)
    assert report.plan_identity_sha256
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


def test_verify_inventory_revision_forwards_repo_inventory_and_revision() -> None:
    inventory = (UploadItem("data/a.parquet", 1, "a"),)
    calls: list[tuple[object, ...]] = []

    def verify(repo_id: str, files: tuple[UploadItem, ...], *, revision: str) -> str:
        calls.append((repo_id, files, revision))
        return "data-revision"

    assert release_module._verify_inventory_revision(verify, REPO_ID, inventory, "parent") == (
        "data-revision"
    )
    assert calls == [(REPO_ID, inventory, "parent")]


def test_verify_inventory_revision_rejects_an_empty_revision_exactly() -> None:
    with pytest.raises(
        PublicationError,
        match=exactly("hub inventory verification returned an empty revision"),
    ):
        release_module._verify_inventory_revision(
            lambda *_args, **_kwargs: "", REPO_ID, (), "parent"
        )


def test_matching_metadata_revision_requires_the_exact_remote_arguments() -> None:
    plan = UploadPlan(REPO_ID, "root", (UploadItem("README.md", 1, "a"),), "identity")
    inventory = (UploadItem("data/a.parquet", 1, "b"),)
    calls: list[tuple[object, ...]] = []

    class Verifier:
        def matching_revision(self, repo_id: str, files: tuple[UploadItem, ...]) -> str:
            calls.append((repo_id, files))
            return "metadata-revision"

    def verify(repo_id: str, files: tuple[UploadItem, ...], *, revision: str) -> str:
        calls.append((repo_id, files, revision))
        return "data-revision"

    assert release_module._matching_metadata_revision(plan, inventory, Verifier(), verify) == (
        "data-revision",
        "metadata-revision",
    )
    assert calls == [
        (REPO_ID, plan.files),
        (REPO_ID, inventory, "metadata-revision"),
    ]


def test_matching_metadata_revision_skips_verifiers_without_matching_support() -> None:
    plan = UploadPlan(REPO_ID, "root", (), "identity")

    assert (
        release_module._matching_metadata_revision(plan, (), object(), lambda *_args: "unused")
        is None
    )


def test_verify_metadata_revision_forwards_inventory_and_refuses_mismatch() -> None:
    plan = UploadPlan(REPO_ID, "root", (), "identity")
    inventory = (UploadItem("data/a.parquet", 1, "a"),)
    calls: list[tuple[object, ...]] = []

    class Verifier:
        def __call__(self, repo_id: str, files: tuple[UploadItem, ...]) -> str:
            calls.append((repo_id, files))
            return "metadata-revision"

    def verify(repo_id: str, files: tuple[UploadItem, ...], *, revision: str) -> str:
        calls.append((repo_id, files, revision))
        return "metadata-revision"

    assert release_module._verify_metadata_revision(plan, inventory, Verifier(), verify) == (
        "metadata-revision"
    )
    assert calls == [
        (REPO_ID, plan.files),
        (REPO_ID, inventory, "metadata-revision"),
    ]

    def diverge(_repo_id: str, _files: tuple[UploadItem, ...], *, revision: str) -> str:
        return f"{revision}-other"

    with pytest.raises(
        PublicationError,
        match=exactly("hub inventory verification returned a revision different from the upload"),
    ):
        release_module._verify_metadata_revision(plan, inventory, Verifier(), diverge)


def test_publish_uses_the_data_revision_as_the_optimistic_parent(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    plan = UploadPlan(REPO_ID, "root", (), "identity")
    inventory = (UploadItem("data/a.parquet", 1, "a"),)
    upload_calls: list[dict[str, object]] = []

    class Verifier:
        def __call__(self, _repo_id: str, _files: tuple[UploadItem, ...]) -> str:
            return "metadata-revision"

        def verify_inventory(
            self,
            _repo_id: str,
            _files: tuple[UploadItem, ...],
            *,
            revision: str | None = None,
        ) -> str:
            return revision or "data-revision"

    monkeypatch.setattr(
        release_module,
        "execute_upload",
        lambda actual_plan, **kwargs: upload_calls.append({"plan": actual_plan, **kwargs}),
    )

    assert release_module._publish(
        plan,
        inventory=inventory,
        runner=None,
        verifier=Verifier(),
        data_revision="data-revision",
    ) == ("data-revision", "metadata-revision")
    assert upload_calls == [
        {
            "plan": plan,
            "confirmation": "identity",
            "runner": None,
            "parent_revision": "data-revision",
        }
    ]


def test_publish_if_requested_forwards_the_computed_inventory(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    plan = UploadPlan(REPO_ID, "root", (), "identity")
    inventory = (UploadItem("data/a.parquet", 1, "a"),)
    calls: list[tuple[object, ...]] = []
    context = release_module._RemoteReleaseContext(object(), inventory, "data-revision")

    def publish(*args: object, **kwargs: object) -> tuple[str, str]:
        calls.append((*args, kwargs))
        return "data-revision", "metadata-revision"

    monkeypatch.setattr(release_module, "_publish", publish)

    assert release_module._publish_if_requested(context, plan, inventory, None) == (
        "data-revision",
        "metadata-revision",
    )
    assert calls == [
        (
            plan,
            {
                "inventory": inventory,
                "runner": None,
                "verifier": context.verifier,
                "data_revision": "data-revision",
            },
        )
    ]


def test_legacy_release_boundaries_forward_non_strict_inventory_validation(
    workspace: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    inventory_calls: list[dict[str, object]] = []

    def record_inventory(_root: Path, **kwargs: object) -> tuple[UploadItem, ...]:
        inventory_calls.append(kwargs)
        return (UploadItem("data/region-a.parquet", 1, "a" * 64),)

    monkeypatch.setattr(release_module, "_published_inventory", record_inventory)
    monkeypatch.setattr(
        release_module,
        "generate_dataset_docs",
        lambda *_args, **_kwargs: {"rows": 1},
    )
    monkeypatch.setattr(release_module, "build_metadata_only_upload_plan", lambda _root: object())

    release_module._compute_release_artifacts(workspace, dataset_card_template())
    assert inventory_calls == [{"require_successful_text": False}]


def test_prepare_remote_release_forwards_non_strict_inventory_validation(
    workspace: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    inventory_calls: list[dict[str, object]] = []
    sync_calls: list[tuple[object, ...]] = []

    def record_inventory(_root: Path, **kwargs: object) -> tuple[UploadItem, ...]:
        inventory_calls.append(kwargs)
        return (UploadItem("data/region-a.parquet", 1, "a" * 64),)

    monkeypatch.setattr(release_module, "_published_inventory", record_inventory)
    monkeypatch.setattr(
        release_module,
        "_sync_remote_card",
        lambda *args: sync_calls.append(args),
    )
    verifier = _RecordingVerifier()

    context = release_module._prepare_remote_release(workspace, REPO_ID, verifier)

    assert inventory_calls == [{"require_successful_text": False}]
    assert context.data_revision == "data-revision"
    assert sync_calls == [(workspace, verifier, REPO_ID, "data-revision")]


def test_prepare_remote_release_rejects_an_empty_data_revision_exactly(
    monkeypatch: pytest.MonkeyPatch,
    workspace: Path,
) -> None:
    inventory = (UploadItem("data/a.parquet", 1, "a"),)

    monkeypatch.setattr(release_module, "_published_inventory", lambda *_args, **_kwargs: inventory)
    monkeypatch.setattr(release_module, "_sync_remote_card", lambda *_args: None)

    class Verifier:
        def verify_inventory(
            self,
            _repo_id: str,
            _files: tuple[UploadItem, ...],
            *,
            revision: str | None = None,
        ) -> str:
            return ""

    with pytest.raises(
        PublicationError,
        match=exactly("hub inventory verification returned an empty revision"),
    ):
        release_module._prepare_remote_release(workspace, REPO_ID, Verifier())


def test_published_inventory_forwards_non_strict_text_validation(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    data_root = tmp_path / "generated"
    (data_root / "data").mkdir(parents=True)
    (data_root / "manifests").mkdir()
    calls: list[tuple[Path, dict[str, object]]] = []
    data_item = UploadItem("data/region-a.parquet", 1, "a" * 64)

    def record_data_items(_root: Path, **kwargs: object) -> list[UploadItem]:
        calls.append((_root, kwargs))
        return [data_item]

    monkeypatch.setattr(release_module, "_collect_data_items", record_data_items)
    monkeypatch.setattr(release_module, "_collect_manifest_items", lambda _root: [])

    inventory = release_module._published_inventory(
        data_root,
        require_successful_text=False,
    )

    assert calls == [(data_root, {"require_successful_text": False})]

    calls.clear()
    assert release_module._published_inventory(data_root) == (data_item,)
    assert calls == [(data_root, {"require_successful_text": True})]
    assert inventory == (data_item,)


def test_validate_published_inventory_uses_non_strict_inventory_validation(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[dict[str, object]] = []

    def record_inventory(_root: Path, **kwargs: object) -> tuple[UploadItem, ...]:
        calls.append(kwargs)
        return (UploadItem("data/region-a.parquet", 1, "a" * 64),)

    monkeypatch.setattr(release_module, "_published_inventory", record_inventory)

    assert release_module.validate_published_inventory(tmp_path) == 1
    assert calls == [{"require_successful_text": False}]


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


def test_release_metadata_resolves_a_missing_root_without_requiring_it_to_exist(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    root = tmp_path / "not-created-yet"
    plan = UploadPlan(REPO_ID, str(root.resolve()), (), "identity")
    seen: list[Path] = []

    monkeypatch.setattr(
        release_module,
        "validate_published_inventory",
        lambda actual: seen.append(actual) or 0,
    )
    monkeypatch.setattr(
        release_module,
        "_compute_release_artifacts",
        lambda actual, _template: ({"rows": 0}, plan, ()),
    )

    report = release_module.release_metadata(
        root,
        dataset_card_template(),
        confirm_repo=REPO_ID,
        apply=False,
    )

    assert seen == [root.resolve()]
    assert report.data_root == str(root.resolve())


def test_release_metadata_refuses_a_changed_inventory_with_exact_reason(
    monkeypatch: pytest.MonkeyPatch,
    workspace: Path,
) -> None:
    plan = UploadPlan(REPO_ID, str(workspace), (), "identity")
    old_inventory = (UploadItem("data/old.parquet", 1, "a"),)
    new_inventory = (UploadItem("data/new.parquet", 1, "b"),)
    context = release_module._RemoteReleaseContext(object(), old_inventory, "data-revision")

    monkeypatch.setattr(release_module, "validate_published_inventory", lambda _root: 1)
    monkeypatch.setattr(release_module, "_prepare_remote_release", lambda *_args: context)
    monkeypatch.setattr(
        release_module,
        "_compute_release_artifacts",
        lambda *_args: ({"rows": 1}, plan, new_inventory),
    )

    with pytest.raises(
        PublicationError,
        match=exactly("local data/manifest inventory changed during stats generation"),
    ):
        release_module.release_metadata(
            workspace,
            dataset_card_template(),
            confirm_repo=REPO_ID,
            apply=True,
        )


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

    with pytest.raises(
        PublicationError,
        match=exactly("Hub verifier read_file has an incompatible interface"),
    ):
        release_module._read_remote_card(Reader(), REPO_ID, "revision")


def test_read_remote_card_rejects_non_text_verifier_response() -> None:
    class Reader:
        def read_file(self, repo_id: str, path: str, *, revision: str) -> object:
            return {"repo_id": repo_id, "path": path, "revision": revision}

    with pytest.raises(
        PublicationError,
        match=exactly("Hub verifier returned a non-text README"),
    ):
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


def test_sync_remote_card_preserves_utf8_and_newline_bytes(tmp_path: Path) -> None:
    remote = "réunion\r\nsecond line\r\n"

    class Reader:
        def read_file(self, repo_id: str, path: str, *, revision: str) -> str:
            assert (repo_id, path, revision) == (REPO_ID, "README.md", "revision")
            return remote

    release_module._sync_remote_card(tmp_path, Reader(), REPO_ID, "revision")

    assert (tmp_path / "README.md").read_bytes() == remote.encode("utf-8")
    # Name the entry from the directory listing: ``exists()`` case-folds on APFS.
    cards = [entry.name for entry in tmp_path.iterdir() if entry.name.lower() == "readme.md"]
    assert cards == ["README.md"]


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
    assert H3_MAP_TITLE in updated
    assert H3_MAP_DESCRIPTION in updated
    assert "Hexbin density of every described polygon" not in updated
    assert _without_generated_map_and_stats(updated) == _without_generated_map_and_stats(
        normalize_map_prose(golden)
    )

    stats_start = updated.index("<!-- GENERATED:STATS:START -->")
    stats_end = updated.index("<!-- GENERATED:STATS:END -->", stats_start)
    generated_stats = updated[stats_start : stats_end + len("<!-- GENERATED:STATS:END -->")]
    for expected in (
        "<!-- stats_sha256: ",
        "<!-- stats_schema_version: ",
        "<!-- schema_version: ",
        "## Dataset at a glance",
        "| Regional/raw polygon rows | 3 |",
        (
            "| Canonical globally unique `(osm_type, osm_id)` polygons with "
            "successfully extracted trimmed non-empty description text | 3 |"
        ),
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
        (
            "| Canonical globally unique `(osm_type, osm_id)` polygons with "
            "successfully extracted trimmed non-empty description text | 3 |"
        ),
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

    with pytest.raises(
        PublicationError,
        match=exactly("--apply requires a verifier for the complete data/manifest inventory"),
    ):
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
    with pytest.raises(
        PublicationError,
        match=exactly(f"published data directory missing: {tmp_path / 'data'}"),
    ):
        validate_published_inventory(tmp_path)


def test_empty_inventory_is_refused(tmp_path: Path) -> None:
    (tmp_path / "data").mkdir()
    (tmp_path / "manifests").mkdir()
    with pytest.raises(PublicationError) as error:
        validate_published_inventory(tmp_path)
    assert str(error.value) == f"no published Parquet files under {tmp_path / 'data'}"


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


def test_publish_forwards_the_same_inventory_to_both_verification_steps(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The inventory proves *which* data the metadata was released against.

    Both the pre-upload match and the post-upload verification must see it;
    passing anything else there would verify a revision against nothing.
    """
    plan = UploadPlan(REPO_ID, "root", (), "identity")
    inventory = (UploadItem("data/a.parquet", 1, "a"),)
    seen: list[object] = []

    monkeypatch.setattr(
        release_module,
        "_matching_metadata_revision",
        lambda _plan, items, *_rest: seen.append(("match", items)),
    )
    monkeypatch.setattr(release_module, "execute_upload", lambda *_a, **_k: None)
    monkeypatch.setattr(
        release_module,
        "_verify_metadata_revision",
        lambda _plan, items, *_rest: (seen.append(("verify", items)), "revision")[1],
    )
    monkeypatch.setattr(release_module, "_require_inventory_verifier", lambda verifier: verifier)

    assert release_module._publish(
        plan,
        inventory=inventory,
        runner=None,
        verifier=object(),
        data_revision="data-revision",
    ) == ("data-revision", "revision")
    assert seen == [("match", inventory), ("verify", inventory)]


def test_a_missing_published_directory_is_refused_by_its_exact_name(tmp_path: Path) -> None:
    """The refusal names both what is missing and where it was looked for."""
    with pytest.raises(
        PublicationError,
        match=exactly(f"published data directory missing: {tmp_path / 'data'}"),
    ):
        release_module._published_inventory(tmp_path, require_successful_text=False)


def test_a_missing_published_manifest_directory_is_refused_by_its_exact_name(
    tmp_path: Path,
) -> None:
    data_dir = tmp_path / "data"
    data_dir.mkdir()
    pq.write_table(pa.table({"osm_id": [1]}), data_dir / "region.parquet")
    manifests = tmp_path / "manifests"

    with pytest.raises(
        PublicationError,
        match=exactly(f"published manifest directory missing: {manifests}"),
    ):
        release_module._published_inventory(tmp_path, require_successful_text=False)


def test_an_empty_metadata_revision_is_refused_by_its_exact_message() -> None:
    plan = UploadPlan(REPO_ID, "root", (), "identity")

    with pytest.raises(
        PublicationError,
        match=exactly("hub verification returned an empty revision"),
    ):
        release_module._verify_metadata_revision(
            plan, (), lambda *_a, **_k: "", lambda *_a, **_k: ""
        )
