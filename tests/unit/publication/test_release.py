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
    UploadItem,
    UploadPlan,
)
from osm_polygon_description_tag.publication import release as release_module
from osm_polygon_description_tag.runtime.resources import dataset_card_template
from tests.helpers.messages import exactly
from tests.helpers.release import (
    RecordingVerifier as _RecordingVerifier,
)
from tests.helpers.release import (
    release as _release,
)
from tests.helpers.release import (
    release_workspace as _release_workspace_fixture,  # noqa: F401
)


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
