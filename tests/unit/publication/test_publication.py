import subprocess
from dataclasses import replace
from pathlib import Path

import pytest
from shapely.geometry import Polygon

from osm_polygon_description_tag.manifest import (
    Manifest,
    RunCounts,
    output_identity_for,
    source_identity_for,
    write_manifest,
)
from osm_polygon_description_tag.publication import (
    PublicationError,
    create_upload_plan,
    execute_upload,
    planning,
)
from osm_polygon_description_tag.publication.planning import (
    _build_item,
    _build_metadata_only_upload_plan,
    _build_per_pbf_upload_plan,
    _collect_data_items,
    _collect_manifest_items,
    _read_manifest_for_publication,
    _require_assets_directory_for_plan,
    _require_core_assets,
    _require_h3_map,
    _require_matching_parquet,
    _require_supported_manifest_version,
    _validate_asset_entry,
    _validate_assets_directory,
    _validate_assets_for_publication,
    _validate_data_entry,
    _validate_data_root,
    _validate_local_work,
    _validate_manifest,
    _validate_manifest_entry,
    _validate_publication_parquet,
    _validate_top_level_entries,
    _validate_uploader_cache,
    file_sha256_bytes,
)
from osm_polygon_description_tag.storage import write_geoparquet
from tests.conftest import make_record_dict


def _make_dataset(data_root: Path) -> None:
    (data_root / "data").mkdir(parents=True)
    (data_root / "manifests").mkdir(parents=True)
    (data_root / "README.md").write_text("# Card\n", encoding="utf-8")
    (data_root / "stats.json").write_text("{}\n", encoding="utf-8")
    (data_root / "assets").mkdir()
    (data_root / "assets" / "description_polygon_density.png").write_bytes(
        b"\x89PNG\r\n\x1a\n" + b"map" * 1024
    )
    (data_root / "assets" / "area_distribution.png").write_bytes(
        b"\x89PNG\r\n\x1a\n" + b"hist" * 1024
    )
    source_root = data_root.parent / "raw"
    source_root.mkdir(exist_ok=True)
    source = source_root / "a-latest.osm.pbf"
    source.write_bytes(b"a-latest-bytes")
    record = make_record_dict(
        Polygon([(0, 0), (0, 1), (1, 1), (1, 0)]),
        {"description": "x"},
        osm_id=1,
        source_pbf="a-latest.osm.pbf",
    )
    output = data_root / "data" / "a-latest.parquet"
    write_geoparquet(iter([record]), output, batch_size=10)
    manifest = Manifest(
        manifest_schema_version=2,
        schema_version=3,
        geoparquet_version="1.1.0",
        transform_algorithm_version=3,
        output_algorithm_revision="x" * 64,
        area_policy_sha256="0" * 64,
        source=source_identity_for(source),
        output=output_identity_for(output),
        osmium_version="osmium version 1.16.0",
        dependency_versions={"pyarrow": "20.0.0"},
        code_revision="abc123",
        started_at="2026-07-27T00:00:00+00:00",
        completed_at="2026-07-27T00:01:00+00:00",
        counts=RunCounts(emitted_features=1, included_rows=1, rejections={}),
    )
    write_manifest(manifest, data_root / "manifests" / "a-latest.manifest.json")


def test_create_upload_plan_lists_allowlisted_files(tmp_path: Path) -> None:
    data_root = tmp_path / "generated"
    _make_dataset(data_root)

    plan = create_upload_plan(data_root)

    assert [item.relative_path for item in plan.files] == [
        "README.md",
        "assets/area_distribution.png",
        "assets/description_polygon_density.png",
        "data/a-latest.parquet",
        "manifests/a-latest.manifest.json",
        "stats.json",
    ]
    assert plan.repo_id == "NoeFlandre/osm-polygon-description-tag"
    assert len(plan.identity_sha256) == 64
    assert plan.data_root == str(data_root.resolve(strict=False))


def test_create_upload_plan_is_deterministic(tmp_path: Path) -> None:
    data_root = tmp_path / "generated"
    _make_dataset(data_root)

    plan_a = create_upload_plan(data_root)
    plan_b = create_upload_plan(data_root)

    assert plan_a.identity_sha256 == plan_b.identity_sha256
    assert plan_a.to_json() == plan_b.to_json()


def test_create_upload_plan_rejects_unknown_top_level(tmp_path: Path) -> None:
    data_root = tmp_path / "generated"
    _make_dataset(data_root)
    (data_root / "debug.txt").write_text("debug", encoding="utf-8")

    with pytest.raises(PublicationError, match="top-level|unknown"):
        create_upload_plan(data_root)


def test_create_upload_plan_rejects_symlinks(tmp_path: Path) -> None:
    data_root = tmp_path / "generated"
    _make_dataset(data_root)
    target = tmp_path / "external.bin"
    target.write_bytes(b"x")
    (data_root / "data" / "link.parquet").symlink_to(target)

    with pytest.raises(PublicationError, match="symlink"):
        create_upload_plan(data_root)


def test_create_upload_plan_rejects_temporary_files(tmp_path: Path) -> None:
    data_root = tmp_path / "generated"
    _make_dataset(data_root)
    (data_root / "data" / "leftover.tmp").write_bytes(b"x")

    with pytest.raises(PublicationError, match="temporary|unknown"):
        create_upload_plan(data_root)


def test_create_upload_plan_rejects_missing_card_or_stats(tmp_path: Path) -> None:
    data_root = tmp_path / "generated"
    _make_dataset(data_root)
    (data_root / "README.md").unlink()

    with pytest.raises(PublicationError, match="missing|R|README"):
        create_upload_plan(data_root)


def test_collection_helpers_preserve_allowlist_boundaries(tmp_path: Path) -> None:
    data_root = tmp_path / "generated"
    _make_dataset(data_root)
    (data_root / ".DS_Store").write_bytes(b"Finder metadata")
    (data_root / ".cache" / "huggingface").mkdir(parents=True)

    _validate_top_level_entries(data_root)

    assert [item.relative_path for item in _collect_data_items(data_root)] == [
        "data/a-latest.parquet"
    ]
    assert [item.relative_path for item in _collect_manifest_items(data_root)] == [
        "manifests/a-latest.manifest.json"
    ]


def test_build_item_preserves_non_regular_file_error(tmp_path: Path) -> None:
    directory = tmp_path / "not-a-file"
    directory.mkdir()

    with pytest.raises(PublicationError, match="not a regular file"):
        _build_item(directory, "data/not-a-file")


def test_validate_data_root_rejects_a_file(tmp_path: Path) -> None:
    root = tmp_path / "data-root"
    root.write_bytes(b"not a directory")

    with pytest.raises(PublicationError, match="data root is not a regular directory"):
        _validate_data_root(root)


def test_validate_data_root_rejects_a_directory_symlink(tmp_path: Path) -> None:
    real_root = tmp_path / "real-root"
    real_root.mkdir()
    link = tmp_path / "data-root"
    try:
        link.symlink_to(real_root)
    except OSError:
        pytest.skip("filesystem does not support symlinks")

    with pytest.raises(PublicationError, match="data root is not a regular directory"):
        _validate_data_root(link)


def test_validate_uploader_cache_accepts_only_real_huggingface_child(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    original_is_dir = Path.is_dir

    def case_sensitive_is_dir(path: Path) -> bool:
        if path.name == "HUGGINGFACE":
            return False
        return original_is_dir(path)

    monkeypatch.setattr(Path, "is_dir", case_sensitive_is_dir)
    cache = tmp_path / ".cache"
    cache.mkdir()
    (cache / "huggingface").mkdir()
    _validate_uploader_cache(cache)

    (cache / "huggingface").rmdir()
    with pytest.raises(PublicationError, match="real huggingface cache directory"):
        _validate_uploader_cache(cache)

    outside = tmp_path / "outside"
    outside.mkdir()
    try:
        (cache / "huggingface").symlink_to(outside)
    except OSError:
        pytest.skip("filesystem does not support symlinks")
    with pytest.raises(PublicationError, match="real huggingface cache directory"):
        _validate_uploader_cache(cache)


def test_validate_uploader_cache_rejects_bad_cache_entry(tmp_path: Path) -> None:
    cache_file = tmp_path / ".cache"
    cache_file.write_bytes(b"not a directory")
    with pytest.raises(PublicationError, match="uploader cache must be a directory"):
        _validate_uploader_cache(cache_file)

    real_cache = tmp_path / "real-cache"
    real_cache.mkdir()
    cache_link = tmp_path / "linked-cache"
    try:
        cache_link.symlink_to(real_cache)
    except OSError:
        pytest.skip("filesystem does not support symlinks")
    with pytest.raises(PublicationError, match="uploader cache must be a real directory"):
        _validate_uploader_cache(cache_link)


def test_validate_local_work_requires_a_real_directory(tmp_path: Path) -> None:
    work = tmp_path / ".work"
    work.mkdir()
    _validate_local_work(work)

    work_file = tmp_path / "work-file"
    work_file.write_bytes(b"not a directory")
    with pytest.raises(PublicationError, match="local work path must be a real directory"):
        _validate_local_work(work_file)

    real_work = tmp_path / "real-work"
    real_work.mkdir()
    work_link = tmp_path / "work-link"
    try:
        work_link.symlink_to(real_work)
    except OSError:
        pytest.skip("filesystem does not support symlinks")
    with pytest.raises(PublicationError, match="local work path must be a real directory"):
        _validate_local_work(work_link)


def test_require_assets_directory_preserves_missing_file_and_symlink_errors(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    data_root = tmp_path / "generated"
    data_root.mkdir()
    assets = data_root / "assets"
    assets.mkdir()
    original_exists = Path.exists

    def case_sensitive_exists(path: Path) -> bool:
        if path.name == "ASSETS":
            return False
        return original_exists(path)

    monkeypatch.setattr(Path, "exists", case_sensitive_exists)
    assert _require_assets_directory_for_plan(data_root) == assets
    assets.rmdir()
    with pytest.raises(PublicationError, match="assets directory missing for plan"):
        _require_assets_directory_for_plan(data_root)

    assets_file = data_root / "assets"
    assets_file.write_bytes(b"not a directory")
    with pytest.raises(PublicationError, match="real directory, not a symlink or file"):
        _require_assets_directory_for_plan(data_root)

    assets_file.unlink()
    real_assets = tmp_path / "real-assets"
    real_assets.mkdir()
    try:
        assets_file.symlink_to(real_assets)
    except OSError:
        pytest.skip("filesystem does not support symlinks")
    with pytest.raises(PublicationError, match="real directory, not a symlink or file"):
        _require_assets_directory_for_plan(data_root)


def test_validate_data_entry_rejects_hidden_temporary_and_unexpected_files(
    tmp_path: Path,
) -> None:
    hidden = tmp_path / ".hidden.parquet"
    hidden.write_bytes(b"hidden")
    with pytest.raises(PublicationError, match="temporary or hidden file"):
        _validate_data_entry(hidden)

    temporary = tmp_path / "leftover.tmp"
    temporary.write_bytes(b"temporary")
    with pytest.raises(PublicationError, match="temporary or hidden file"):
        _validate_data_entry(temporary)

    unexpected = tmp_path / "notes.txt"
    unexpected.write_bytes(b"unexpected")
    with pytest.raises(PublicationError, match="unexpected file in data/"):
        _validate_data_entry(unexpected)


def test_collect_data_items_rejects_case_drift_in_data_and_manifest_paths(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    data_root = tmp_path / "generated"
    _make_dataset(data_root)
    original_is_dir = Path.is_dir
    original_is_file = Path.is_file
    original_read_text = Path.read_text

    def case_sensitive_is_dir(path: Path) -> bool:
        if path.name in {"DATA", "MANIFESTS"}:
            return False
        return original_is_dir(path)

    def case_sensitive_is_file(path: Path) -> bool:
        if path.parent.name == "MANIFESTS":
            return False
        return original_is_file(path)

    def case_sensitive_read_text(path: Path, *args: object, **kwargs: object) -> str:
        if path.parent.name == "MANIFESTS" or path.name.endswith(".MANIFEST.JSON"):
            raise FileNotFoundError(path)
        return original_read_text(path, *args, **kwargs)

    monkeypatch.setattr(Path, "is_dir", case_sensitive_is_dir)
    monkeypatch.setattr(Path, "is_file", case_sensitive_is_file)
    monkeypatch.setattr(Path, "read_text", case_sensitive_read_text)

    assert [item.relative_path for item in _collect_data_items(data_root)] == [
        "data/a-latest.parquet"
    ]


def test_validate_manifest_entry_preserves_rejection_contract(tmp_path: Path) -> None:
    hidden = tmp_path / ".hidden.manifest.json"
    hidden.write_bytes(b"hidden")
    with pytest.raises(PublicationError, match="temporary or hidden file"):
        _validate_manifest_entry(hidden)

    temporary = tmp_path / "leftover.tmp"
    temporary.write_bytes(b"temporary")
    with pytest.raises(PublicationError, match="temporary or hidden file"):
        _validate_manifest_entry(temporary)

    unexpected = tmp_path / "manifest.json"
    unexpected.write_bytes(b"unexpected")
    with pytest.raises(PublicationError, match="unexpected file in manifests/"):
        _validate_manifest_entry(unexpected)


def test_collect_manifest_items_does_not_scan_case_variant_directory(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    data_root = tmp_path / "generated"
    _make_dataset(data_root)
    original_is_dir = Path.is_dir

    def case_sensitive_is_dir(path: Path) -> bool:
        if path.name == "MANIFESTS":
            return False
        return original_is_dir(path)

    monkeypatch.setattr(Path, "is_dir", case_sensitive_is_dir)

    assert [item.relative_path for item in _collect_manifest_items(data_root)] == [
        "manifests/a-latest.manifest.json"
    ]


def test_h3_compatibility_helper_requires_the_canonical_map(tmp_path: Path) -> None:
    data_root = tmp_path / "generated"
    assets = data_root / "assets"
    assets.mkdir(parents=True)
    map_path = assets / "description_polygon_density.png"
    map_path.write_bytes(b"map")

    item = _require_h3_map(data_root)
    assert item.relative_path == "assets/description_polygon_density.png"

    map_path.unlink()
    with pytest.raises(PublicationError, match="required file missing for H3 map"):
        _require_h3_map(data_root)


def test_per_pbf_plan_preserves_identity_and_validation_contract(tmp_path: Path) -> None:
    data_root = tmp_path / "generated"
    _make_dataset(data_root)
    (data_root / "assets" / "dataset-card-hero.png").write_bytes(b"hero")

    plan = _build_per_pbf_upload_plan(data_root, "a-latest.osm.pbf")
    provisional = replace(plan, identity_sha256="")
    assert plan.repo_id == "NoeFlandre/osm-polygon-description-tag"
    assert plan.data_root == str(data_root.resolve(strict=False))
    assert plan.identity_sha256 == file_sha256_bytes(provisional.to_json().encode("utf-8"))

    with pytest.raises(PublicationError, match="invalid source name"):
        _build_per_pbf_upload_plan(data_root, "a-latest.pbf")

    (data_root / "data" / "a-latest.parquet").unlink()
    with pytest.raises(PublicationError, match="required file missing for per-PBF plan"):
        _build_per_pbf_upload_plan(data_root, "a-latest.osm.pbf")


def test_metadata_plan_preserves_identity_and_missing_file_contract(tmp_path: Path) -> None:
    data_root = tmp_path / "generated"
    _make_dataset(data_root)
    (data_root / "assets" / "dataset-card-hero.png").write_bytes(b"hero")

    plan = _build_metadata_only_upload_plan(data_root)
    provisional = replace(plan, identity_sha256="")
    assert plan.repo_id == "NoeFlandre/osm-polygon-description-tag"
    assert plan.data_root == str(data_root.resolve(strict=False))
    assert plan.identity_sha256 == file_sha256_bytes(provisional.to_json().encode("utf-8"))

    (data_root / "README.md").unlink()
    with pytest.raises(PublicationError, match="required file missing for metadata plan"):
        _build_metadata_only_upload_plan(data_root)


def _write_assets(assets_dir: Path, *names: str) -> None:
    assets_dir.mkdir(parents=True, exist_ok=True)
    for name in names:
        (assets_dir / name).write_bytes(b"asset")


def test_publication_validation_helpers_preserve_their_contracts(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    data_root = tmp_path / "generated"
    _make_dataset(data_root)
    hero = data_root / "assets" / "dataset-card-hero.png"
    hero.write_bytes(b"hero")
    manifest_path = data_root / "manifests" / "a-latest.manifest.json"
    parquet_path = data_root / "data" / "a-latest.parquet"
    manifest = _read_manifest_for_publication(manifest_path)

    _require_supported_manifest_version(manifest)
    _require_matching_parquet(manifest, manifest_path, parquet_path)
    _validate_publication_parquet(parquet_path)
    asset = _validate_asset_entry(data_root / "assets" / "dataset-card-hero.png")
    assets = _validate_assets_directory(data_root / "assets")
    _require_core_assets(assets)

    published_assets = _validate_assets_for_publication(data_root)

    assert asset.relative_path == "assets/dataset-card-hero.png"
    assert [item.relative_path for item in published_assets] == [
        "assets/description_polygon_density.png",
        "assets/area_distribution.png",
        "assets/dataset-card-hero.png",
    ]

    captured_paths: list[Path] = []
    monkeypatch.setattr(
        planning,
        "_validate_assets_directory",
        lambda path: captured_paths.append(path) or assets,
    )
    _validate_assets_for_publication(data_root)
    assert captured_paths == [data_root / "assets"]

    monkeypatch.setattr(
        planning,
        "read_manifest",
        lambda _path: (_ for _ in ()).throw(planning.ManifestError("bad manifest")),
    )
    with pytest.raises(PublicationError, match="invalid manifest"):
        _read_manifest_for_publication(manifest_path)

    monkeypatch.setattr(planning, "read_manifest", lambda _path: manifest)
    mismatched = replace(manifest, output=replace(manifest.output, sha256="0" * 64))
    with pytest.raises(PublicationError, match="output identity"):
        _require_matching_parquet(mismatched, manifest_path, parquet_path)

    monkeypatch.setattr(planning, "read_manifest", lambda _path: mismatched)
    with pytest.raises(PublicationError) as error:
        _validate_manifest(manifest_path, parquet_path)
    assert str(error.value) == f"manifest output identity does not match parquet: {manifest_path}"


def test_publication_asset_requirements_report_exact_missing_files(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    data_root = tmp_path / "generated"
    assets_dir = data_root / "assets"
    _write_assets(
        assets_dir,
        "description_polygon_density.png",
        "area_distribution.png",
        "dataset-card-hero.png",
    )
    h3 = _validate_asset_entry(assets_dir / "description_polygon_density.png")
    histogram = _validate_asset_entry(assets_dir / "area_distribution.png")
    hero = _validate_asset_entry(assets_dir / "dataset-card-hero.png")

    monkeypatch.setattr(planning, "_validate_assets_directory", lambda _path: [h3])
    with pytest.raises(PublicationError) as error:
        _validate_assets_for_publication(data_root)
    assert str(error.value) == (
        "assets directory must contain the area distribution histogram at "
        "assets/area_distribution.png"
    )

    monkeypatch.setattr(planning, "_validate_assets_directory", lambda _path: [h3, histogram])
    with pytest.raises(PublicationError) as error:
        _validate_assets_for_publication(data_root)
    assert str(error.value) == (
        "assets directory must contain the dataset card hero at assets/dataset-card-hero.png"
    )

    with pytest.raises(PublicationError) as error:
        _require_core_assets([h3])
    assert str(error.value) == (
        "assets directory must contain the area distribution histogram at "
        "assets/area_distribution.png"
    )

    assert hero.relative_path == "assets/dataset-card-hero.png"


@pytest.mark.parametrize(
    ("name", "message"),
    [
        (".hidden", "hidden file under assets/ not allowed"),
        ("leftover.tmp", "temporary file under assets/ not allowed"),
        ("unrelated.png", "unrelated file under assets/ not allowed"),
    ],
)
def test_assets_validation_reports_each_rejected_filename(
    tmp_path: Path, name: str, message: str
) -> None:
    assets_dir = tmp_path / "assets"
    _write_assets(assets_dir, name)

    with pytest.raises(PublicationError, match=message):
        _validate_assets_directory(assets_dir)


def test_assets_validation_requires_a_real_directory_and_both_required_maps(tmp_path: Path) -> None:
    missing = tmp_path / "missing-assets"
    with pytest.raises(PublicationError, match=f"assets directory missing: {missing}"):
        _validate_assets_directory(missing)

    assets_dir = tmp_path / "assets"
    _write_assets(assets_dir, "area_distribution.png", "dataset-card-hero.png")
    with pytest.raises(PublicationError) as error:
        _validate_assets_directory(assets_dir)
    assert str(error.value) == (
        "assets directory must contain the H3 density map at assets/description_polygon_density.png"
    )

    (assets_dir / "description_polygon_density.png").write_bytes(b"map")
    (assets_dir / "area_distribution.png").unlink()
    with pytest.raises(PublicationError) as error:
        _validate_assets_directory(assets_dir)
    assert str(error.value) == (
        "assets directory must contain the area distribution histogram at "
        "assets/area_distribution.png"
    )


def test_assets_validation_returns_items_in_filename_order(tmp_path: Path) -> None:
    assets_dir = tmp_path / "assets"
    _write_assets(
        assets_dir,
        "description_polygon_density.png",
        "dataset-card-hero.png",
        "area_distribution.png",
    )

    items = _validate_assets_directory(assets_dir)

    assert [item.relative_path for item in items] == [
        "assets/area_distribution.png",
        "assets/dataset-card-hero.png",
        "assets/description_polygon_density.png",
    ]


def test_assets_validation_rejects_symlink_and_non_regular_entries(tmp_path: Path) -> None:
    assets_dir = tmp_path / "assets"
    _write_assets(assets_dir, "description_polygon_density.png")
    target = tmp_path / "target.png"
    target.write_bytes(b"target")
    try:
        (assets_dir / "area_distribution.png").symlink_to(target)
    except OSError:
        pytest.skip("filesystem does not support symlinks")

    with pytest.raises(PublicationError, match="symlink not allowed under assets/"):
        _validate_assets_directory(assets_dir)

    (assets_dir / "area_distribution.png").unlink()
    (assets_dir / "area_distribution.png").mkdir()
    with pytest.raises(PublicationError, match="not a regular file under assets/"):
        _validate_assets_directory(assets_dir)


def test_assets_validation_rejects_symlinked_directory(tmp_path: Path) -> None:
    real_assets = tmp_path / "real-assets"
    _write_assets(
        real_assets,
        "area_distribution.png",
        "dataset-card-hero.png",
        "description_polygon_density.png",
    )
    assets_dir = tmp_path / "assets"
    try:
        assets_dir.symlink_to(real_assets, target_is_directory=True)
    except OSError:
        pytest.skip("filesystem does not support symlinks")

    with pytest.raises(PublicationError, match="assets directory must be a real directory"):
        _validate_assets_directory(assets_dir)


def test_manifest_validation_reports_each_publication_failure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    data_root = tmp_path / "generated"
    _make_dataset(data_root)
    manifest_path = data_root / "manifests" / "a-latest.manifest.json"
    parquet_path = data_root / "data" / "a-latest.parquet"
    manifest = planning.read_manifest(manifest_path)

    monkeypatch.setattr(
        planning,
        "read_manifest",
        lambda _path: replace(manifest, manifest_schema_version=999),
    )
    with pytest.raises(PublicationError, match="manifest uses unsupported schema version: 999"):
        _validate_manifest(manifest_path, parquet_path)

    monkeypatch.setattr(planning, "read_manifest", lambda _path: manifest)
    with pytest.raises(PublicationError, match="parquet missing for manifest"):
        _validate_manifest(manifest_path, tmp_path / "missing.parquet")

    monkeypatch.setattr(
        planning,
        "validate_geoparquet",
        lambda _path: (_ for _ in ()).throw(planning.StorageError("invalid rows")),
    )
    with pytest.raises(
        PublicationError, match="parquet fails validation for publication: invalid rows"
    ):
        _validate_manifest(manifest_path, parquet_path)


def test_execute_upload_refuses_wrong_confirmation(tmp_path: Path) -> None:
    data_root = tmp_path / "generated"
    _make_dataset(data_root)
    plan = create_upload_plan(data_root)

    def runner(command: list[str]) -> None:
        raise AssertionError("runner should not be invoked")

    with pytest.raises(PublicationError, match="confirmation"):
        execute_upload(plan, confirmation="deadbeef", runner=runner)


def test_execute_upload_passes_allowlisted_exact_includes(tmp_path: Path) -> None:
    data_root = tmp_path / "generated"
    _make_dataset(data_root)
    plan = create_upload_plan(data_root)

    captured: list[list[str]] = []

    def runner(command: list[str]) -> None:
        captured.append(command)

    execute_upload(plan, confirmation=plan.identity_sha256, runner=runner)

    assert len(captured) == 1
    expected_command = [
        "hf",
        "upload-large-folder",
        "NoeFlandre/osm-polygon-description-tag",
        str(data_root.resolve(strict=False)),
        "--repo-type",
        "dataset",
    ]
    for item in sorted(plan.files, key=lambda i: i.relative_path):
        expected_command.extend(["--include", item.relative_path])
    assert captured[0] == expected_command


def test_execute_upload_detects_checksum_drift(tmp_path: Path) -> None:
    data_root = tmp_path / "generated"
    _make_dataset(data_root)
    plan = create_upload_plan(data_root)

    # Mutate an artifact after plan creation.
    (data_root / "README.md").write_text("mutated", encoding="utf-8")

    def runner(command: list[str]) -> None:
        raise AssertionError("runner should not be invoked on drift")

    with pytest.raises(PublicationError, match="drift|mismatch"):
        execute_upload(plan, confirmation=plan.identity_sha256, runner=runner)


def test_execute_upload_invokes_runner_with_subprocess_run_by_default(tmp_path: Path) -> None:
    data_root = tmp_path / "generated"
    _make_dataset(data_root)
    plan = create_upload_plan(data_root)

    def fake_subprocess_run(command, **kwargs):  # type: ignore[no-untyped-def]
        return subprocess.CompletedProcess(command, 0)

    # Patch subprocess.run via the publication module import path.
    import osm_polygon_description_tag.publication.upload as publication

    original = publication.subprocess.run
    publication.subprocess.run = fake_subprocess_run  # type: ignore[assignment]
    try:
        # When invoked with the default runner, confirm it goes through subprocess.run.
        execute_upload(plan, confirmation=plan.identity_sha256)
    finally:
        publication.subprocess.run = original  # type: ignore[assignment]
