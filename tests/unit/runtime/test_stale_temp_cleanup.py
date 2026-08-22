"""Safe cleanup of abandoned atomic-write temporaries."""

from __future__ import annotations

import os
from pathlib import Path

import pytest


def test_cleanup_removes_only_owned_temp_older_than_final_target(tmp_path: Path) -> None:
    from osm_polygon_description_tag.runtime.cleanup import cleanup_stale_owned_temps

    data_root = tmp_path / "generated"
    data_dir = data_root / "data"
    data_dir.mkdir(parents=True)
    target = data_dir / "region.parquet"
    target.write_bytes(b"final")

    stale = data_dir / ".region.parquet.0123456789abcdef0123456789abcdef.tmp"
    stale.write_bytes(b"stale")
    newer = data_dir / ".region.parquet.fedcba9876543210fedcba9876543210.tmp"
    newer.write_bytes(b"newer")
    arbitrary = data_dir / ".region.parquet.not-a-uuid.tmp"
    arbitrary.write_bytes(b"arbitrary")
    wrong_target = data_dir / ".region.txt.0123456789abcdef0123456789abcdef.tmp"
    wrong_target.write_bytes(b"wrong-target")
    targetless = data_dir / ".missing.parquet.0123456789abcdef0123456789abcdef.tmp"
    targetless.write_bytes(b"targetless")

    os.utime(stale, ns=(target.stat().st_mtime_ns - 2, target.stat().st_mtime_ns - 2))
    os.utime(newer, ns=(target.stat().st_mtime_ns + 2, target.stat().st_mtime_ns + 2))

    removed = cleanup_stale_owned_temps(data_root)

    assert removed == (stale,)
    assert not stale.exists()
    assert newer.is_file()
    assert arbitrary.is_file()
    assert wrong_target.is_file()
    assert targetless.is_file()


def test_cleanup_private_name_and_target_guards_are_explicit(tmp_path: Path) -> None:
    import osm_polygon_description_tag.runtime.cleanup as cleanup

    data_dir = tmp_path / "data"
    manifests_dir = tmp_path / "manifests"
    other_dir = tmp_path / "other"
    data_dir.mkdir()
    manifests_dir.mkdir()
    other_dir.mkdir()

    parquet_temp = data_dir / ".a.parquet.0123456789abcdef0123456789abcdef.tmp"
    parquet_temp.write_bytes(b"x")
    manifest_temp = manifests_dir / ".a.manifest.json.0123456789abcdef0123456789abcdef.tmp"
    manifest_temp.write_bytes(b"x")
    ignored_temp = data_dir / ".a.txt.0123456789abcdef0123456789abcdef.tmp"
    ignored_temp.write_bytes(b"x")

    assert cleanup._regular_file(parquet_temp)
    assert cleanup._extract_target_name(parquet_temp) == "a.parquet"
    assert cleanup._candidate_target_name(parquet_temp, None) == "a.parquet"
    assert cleanup._candidate_target_name(parquet_temp, {"other.parquet"}) is None
    assert cleanup._target_name_allowed(data_dir, "a.parquet")
    assert not cleanup._target_name_allowed(data_dir, "a.txt")
    assert cleanup._target_name_allowed(manifests_dir, "a.manifest.json")
    assert not cleanup._target_name_allowed(manifests_dir, "a.parquet")
    assert cleanup._target_name_allowed(other_dir, "a.parquet")
    assert cleanup._eligible_target(data_dir, parquet_temp, None) is None
    assert cleanup._eligible_target(data_dir, ignored_temp, None) is None
    assert cleanup._cleanup_directory(other_dir, None) == ()

    target = data_dir / "a.parquet"
    target.write_bytes(b"final")
    target_time = target.stat().st_mtime_ns
    import os

    os.utime(parquet_temp, ns=(target_time - 1, target_time - 1))
    assert cleanup._target_is_newer(parquet_temp, target)
    assert cleanup._eligible_target(data_dir, parquet_temp, {"other.parquet"}) is None
    assert cleanup._remove_if_stale(data_dir, parquet_temp, None) == parquet_temp
    assert not parquet_temp.exists()


def test_cleanup_routes_exact_targets_and_sorts_all_removed_paths(tmp_path: Path) -> None:
    from osm_polygon_description_tag.runtime.cleanup import cleanup_stale_owned_temps

    data_root = tmp_path / "generated"
    data_dir = data_root / "data"
    manifests_dir = data_root / "manifests"
    data_dir.mkdir(parents=True)
    manifests_dir.mkdir()

    targets = [
        data_root / "stats.json",
        data_root / "README.md",
        data_dir / "a.parquet",
        data_dir / "b.parquet",
        manifests_dir / "a.manifest.json",
    ]
    for target in targets:
        target.write_bytes(b"final")

    candidates = [
        target.parent / f".{target.name}.0123456789abcdef0123456789abcdef.tmp" for target in targets
    ]
    for candidate, target in zip(candidates, targets, strict=True):
        candidate.write_bytes(b"stale")
        old = target.stat().st_mtime_ns - 1
        os.utime(candidate, ns=(old, old))

    removed = cleanup_stale_owned_temps(data_root)

    assert removed == tuple(sorted(candidates, key=lambda path: str(path)))
    assert all(not candidate.exists() for candidate in candidates)


def test_cleanup_root_directory_preserves_exact_target_allowlist(tmp_path: Path) -> None:
    import osm_polygon_description_tag.runtime.cleanup as cleanup

    root = tmp_path / "generated"
    root.mkdir()
    allowed = root / "README.md"
    allowed.write_bytes(b"final")
    allowed_temp = root / ".README.md.0123456789abcdef0123456789abcdef.tmp"
    allowed_temp.write_bytes(b"stale")
    other = root / "notes.json"
    other.write_bytes(b"final")
    other_temp = root / ".notes.json.0123456789abcdef0123456789abcdef.tmp"
    other_temp.write_bytes(b"stale")
    old = allowed.stat().st_mtime_ns - 1
    os.utime(allowed_temp, ns=(old, old))
    os.utime(other_temp, ns=(old, old))

    assert cleanup._cleanup_directory(root, {"README.md"}) == (allowed_temp,)
    assert not allowed_temp.exists()
    assert other_temp.exists()


def test_cleanup_routes_manifest_and_data_directories_exactly(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import osm_polygon_description_tag.runtime.cleanup as cleanup

    calls: list[tuple[Path, set[str] | None]] = []

    def record(directory: Path, exact_targets: set[str] | None) -> tuple[Path, ...]:
        calls.append((directory, exact_targets))
        return ()

    monkeypatch.setattr(cleanup, "_cleanup_directory", record)
    data_root = tmp_path / "generated"
    cleanup.cleanup_stale_owned_temps(data_root)

    assert calls == [
        (data_root, {"README.md", "stats.json", "publication-state.json"}),
        (data_root / "data", None),
        (data_root / "manifests", None),
    ]


def test_cleanup_sorts_paths_independently_of_directory_iteration(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import osm_polygon_description_tag.runtime.cleanup as cleanup

    data_root = tmp_path / "generated"
    returned = (
        (data_root / "z.parquet", data_root / "a.parquet"),
        (data_root / "m.manifest.json",),
        (data_root / "b.json",),
    )
    observed = list(returned)

    def record(_directory: Path, _exact_targets: set[str] | None) -> tuple[Path, ...]:
        return observed.pop(0)

    monkeypatch.setattr(cleanup, "_cleanup_directory", record)

    assert cleanup.cleanup_stale_owned_temps(data_root) == tuple(
        sorted((*returned[0], *returned[1], *returned[2]), key=lambda path: str(path))
    )


def test_cleanup_rejects_symlink_candidates_and_missing_target_names(tmp_path: Path) -> None:
    import osm_polygon_description_tag.runtime.cleanup as cleanup

    data_dir = tmp_path / "data"
    data_dir.mkdir()
    target = data_dir / "target.parquet"
    target.write_bytes(b"final")
    symlink = data_dir / ".target.parquet.0123456789abcdef0123456789abcdef.tmp"
    try:
        symlink.symlink_to(target)
    except (OSError, NotImplementedError):
        pytest.skip("symlinks are unavailable")

    missing_name = data_dir / "not-owned.tmp"
    missing_name.write_bytes(b"candidate")
    assert not cleanup._regular_file(symlink)
    assert cleanup._eligible_target(data_dir, symlink, None) is None
    assert cleanup._eligible_target(data_dir, missing_name, None) is None


def test_cleanup_requires_strictly_older_candidate(tmp_path: Path) -> None:
    import osm_polygon_description_tag.runtime.cleanup as cleanup

    target = tmp_path / "target.parquet"
    candidate = tmp_path / ".target.parquet.0123456789abcdef0123456789abcdef.tmp"
    target.write_bytes(b"final")
    candidate.write_bytes(b"candidate")
    timestamp = target.stat().st_mtime_ns
    os.utime(candidate, ns=(timestamp, timestamp))

    assert not cleanup._target_is_newer(candidate, target)
    assert cleanup._remove_if_stale(tmp_path, candidate, None) is None
    assert candidate.exists()
