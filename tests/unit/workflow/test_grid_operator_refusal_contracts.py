"""Exact refusal wording of the Grid'5000 operator's readers and validators.

Each label below is what names the failing document to an operator holding one
of 386 shards: "cannot read job config ..." and "cannot read stage manifest ..."
are different problems with different fixes. A substring assertion keeps passing
when the label is lost, so every message here is asserted whole.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from osm_polygon_description_tag.dataset.languages.payloads import require_object
from osm_polygon_description_tag.workflow import grid_operator
from osm_polygon_description_tag.workflow.grid_operator import (
    STAGE_MANIFEST_FILENAME,
    GridOperatorError,
    JobBundle,
    read_bundle,
)
from tests.helpers.messages import exactly


def _bundle() -> JobBundle:
    return JobBundle(
        snapshot_id="a" * 64,
        model_config_fingerprint="b" * 64,
        code_fingerprint="c" * 64,
        lock_fingerprint="d" * 64,
        shard="region.parquet",
        source_sha256="e" * 64,
        source_size_bytes=16,
        input_row_count=8,
    )


def test_an_unreadable_job_config_is_named_as_a_job_config(tmp_path: Path) -> None:
    path = tmp_path / "job-config.json"
    path.write_text("{not json", encoding="utf-8")

    with pytest.raises(GridOperatorError) as caught:
        grid_operator._read_job_config(path, _bundle())

    assert str(caught.value).startswith(f"cannot read job config {path}: ")


def test_a_job_config_that_is_not_an_object_is_named_as_a_job_config(tmp_path: Path) -> None:
    path = tmp_path / "job-config.json"
    path.write_text("[]", encoding="utf-8")

    with pytest.raises(GridOperatorError, match=exactly("job config payload must be an object")):
        grid_operator._read_job_config(path, _bundle())


def test_an_unreadable_stage_manifest_is_named_as_a_stage_manifest(tmp_path: Path) -> None:
    payload_root = tmp_path / "payload"
    payload_root.mkdir()
    stage_path = payload_root / STAGE_MANIFEST_FILENAME
    stage_path.write_text("{not json", encoding="utf-8")

    with pytest.raises(GridOperatorError) as caught:
        grid_operator._read_stage_manifest(payload_root, _bundle())

    assert str(caught.value).startswith(f"cannot read stage manifest {stage_path}: ")


def test_a_stage_manifest_that_is_not_an_object_is_named_as_a_stage(tmp_path: Path) -> None:
    payload_root = tmp_path / "payload"
    payload_root.mkdir()
    (payload_root / STAGE_MANIFEST_FILENAME).write_text("[]", encoding="utf-8")

    with pytest.raises(GridOperatorError, match=exactly("stage payload must be an object")):
        grid_operator._read_stage_manifest(payload_root, _bundle())


def test_a_stage_manifest_without_a_readable_fingerprint_reads_as_absent(tmp_path: Path) -> None:
    """The resume fingerprint is read separately, and an unusable manifest means none."""
    payload_root = tmp_path / "payload"
    payload_root.mkdir()
    (payload_root / STAGE_MANIFEST_FILENAME).write_text("[]", encoding="utf-8")

    assert grid_operator._staged_resume_fingerprint(payload_root) is None


def test_a_staged_resume_fingerprint_is_read_from_the_stage_manifest(tmp_path: Path) -> None:
    payload_root = tmp_path / "payload"
    payload_root.mkdir()
    (payload_root / STAGE_MANIFEST_FILENAME).write_text(
        json.dumps({"resume_state_fingerprint": "f" * 64}), encoding="utf-8"
    )

    assert grid_operator._staged_resume_fingerprint(payload_root) == "f" * 64


def test_a_symlinked_stage_manifest_is_refused_rather_than_followed(tmp_path: Path) -> None:
    """A manifest outside the payload could claim any resume state at all."""
    payload_root = tmp_path / "payload"
    payload_root.mkdir()
    external = tmp_path / "external.json"
    external.write_text(json.dumps({"resume_state_fingerprint": "f" * 64}), encoding="utf-8")
    (payload_root / STAGE_MANIFEST_FILENAME).symlink_to(external)

    assert grid_operator._staged_resume_fingerprint(payload_root) is None


def test_an_unreadable_bundle_is_named_as_a_bundle(tmp_path: Path) -> None:
    path = tmp_path / "bundle.json"
    path.write_text("{not json", encoding="utf-8")

    with pytest.raises(GridOperatorError) as caught:
        read_bundle(path)

    assert str(caught.value).startswith(f"cannot read bundle {path}: ")


@pytest.mark.parametrize(
    ("value", "message"),
    [
        ("", "shard must be a non-empty relative path"),
        ("/absolute.parquet", "shard must be relative and portable"),
        ("region.csv", "shard must be a Parquet path"),
    ],
)
def test_an_unusable_shard_path_is_refused_under_the_shard_label(value: str, message: str) -> None:
    with pytest.raises(GridOperatorError, match=exactly(message)):
        grid_operator._validate_relative_parquet(value, "shard")


@pytest.mark.parametrize(
    ("value", "message"),
    [
        ("", "staged file path must be a non-empty relative path"),
        ("/absolute", "staged file path must be relative and portable"),
        ("../escape", "staged file path must not contain traversal"),
    ],
)
def test_an_unusable_staged_file_path_is_refused_under_its_own_label(
    value: str, message: str
) -> None:
    with pytest.raises(GridOperatorError, match=exactly(message)):
        grid_operator._validate_stage_relative_file(value)


def test_an_unusable_resume_fingerprint_is_refused_under_its_own_label() -> None:
    stage = require_object(
        {"resume_state_fingerprint": "not a fingerprint", "files": []},
        error=GridOperatorError,
        label="stage",
    )

    with pytest.raises(
        GridOperatorError,
        match=exactly("resume state fingerprint must be a lowercase SHA-256 hex fingerprint"),
    ):
        grid_operator._stage_descriptors(stage)


@pytest.mark.parametrize(
    ("detector", "model_path", "message"),
    [
        ("lingua", "/models/model_v3.bin", "GlotLID model path requires a cascade snapshot"),
        ("lingua+glotlid-v3-fallback", None, "cascade job requires a GlotLID model path"),
        ("mystery", None, "unsupported detector for Grid jobs: 'mystery'"),
    ],
)
def test_a_grid_job_refuses_a_detector_and_model_path_that_disagree(
    detector: str, model_path: str | None, message: str
) -> None:
    """The compute node loads exactly what this decides, so each refusal is exact."""
    snapshot = _snapshot_with_detector(detector)

    with pytest.raises(GridOperatorError, match=exactly(message)):
        grid_operator._glotlid_model_path_for_snapshot(snapshot, model_path)


def _snapshot_with_detector(detector_name: str) -> object:
    from types import SimpleNamespace

    return SimpleNamespace(model_identity=SimpleNamespace(detector_name=detector_name))


@pytest.mark.parametrize("shape", ["symlink", "missing"])
def test_an_absent_prepared_job_config_is_refused_with_its_exact_reason(
    tmp_path: Path, shape: str
) -> None:
    path = tmp_path / "job-config.json"
    if shape == "symlink":
        path.symlink_to(tmp_path / "elsewhere.json")

    with pytest.raises(
        GridOperatorError, match=exactly("prepared job config is immutable and must be present")
    ):
        grid_operator._verify_immutable_config(path, {})


@pytest.mark.parametrize("shape", ["symlink", "missing"])
def test_an_absent_prepared_job_script_is_refused_with_its_exact_reason(
    tmp_path: Path, shape: str
) -> None:
    path = tmp_path / "job.sh"
    if shape == "symlink":
        path.symlink_to(tmp_path / "elsewhere.sh")

    with pytest.raises(
        GridOperatorError,
        match=exactly("prepared job config is immutable and its script must be present"),
    ):
        grid_operator._verify_immutable_script(path, b"")


def test_a_prepared_job_config_that_changed_is_refused_and_names_the_document(
    tmp_path: Path,
) -> None:
    """Reading it under another label would send the operator to the wrong file."""
    path = tmp_path / "job-config.json"
    path.write_text("{not json", encoding="utf-8")

    with pytest.raises(GridOperatorError) as caught:
        grid_operator._verify_immutable_config(path, {})

    assert str(caught.value).startswith(f"cannot read job config {path}: ")


def test_a_payload_missing_required_files_lists_every_one_of_them() -> None:
    """The operator restages exactly what is listed, so the whole list is the contract."""
    with pytest.raises(GridOperatorError) as caught:
        grid_operator._require_payload_files(_bundle(), {"bundle.json"})

    message = str(caught.value)
    assert message.startswith("portable payload is missing required files: ")
    listed = message.removeprefix("portable payload is missing required files: ").split(", ")
    assert listed == sorted(listed)
    assert "source/region.parquet" in listed


def test_an_unusable_shard_artifact_is_named_by_its_own_file_name(tmp_path: Path) -> None:
    artifact = tmp_path / "part-00000000000000000000.parquet"
    artifact.symlink_to(tmp_path / "elsewhere.parquet")

    with pytest.raises(
        GridOperatorError,
        match=exactly(f"shard state contains an unusable artifact: {artifact.name}"),
    ):
        grid_operator._require_generated_artifact(artifact, lambda _name: True)


def test_an_unrecognised_shard_artifact_is_named_by_its_own_file_name(tmp_path: Path) -> None:
    artifact = tmp_path / "stray.txt"
    artifact.write_text("stray", encoding="utf-8")

    with pytest.raises(
        GridOperatorError,
        match=exactly(f"shard state contains an unknown artifact: {artifact.name}"),
    ):
        grid_operator._require_generated_artifact(artifact, lambda _name: False)


def test_an_unreadable_snapshot_is_reported_under_the_snapshot_readers_own_words(
    tmp_path: Path,
) -> None:
    """Replacing the cause with ``None`` would report the string ``None``."""
    local = tmp_path / "local"
    local.mkdir()
    retrieved = tmp_path / "retrieved"
    retrieved.mkdir()

    with pytest.raises(GridOperatorError) as caught:
        grid_operator._require_matching_snapshots(local, retrieved)

    assert str(caught.value).startswith("cannot read snapshot ")


def test_a_stage_manifest_object_without_the_fingerprint_field_is_refused_by_name(
    tmp_path: Path,
) -> None:
    """An object manifest is a real manifest, so a missing field is an error, not absence."""
    payload_root = tmp_path / "payload"
    payload_root.mkdir()
    (payload_root / STAGE_MANIFEST_FILENAME).write_text(
        json.dumps({"stage_schema_version": 1}), encoding="utf-8"
    )

    with pytest.raises(
        GridOperatorError, match=exactly("stage payload is missing resume_state_fingerprint")
    ):
        grid_operator._staged_resume_fingerprint(payload_root)
