"""Safe cleanup of abandoned atomic-write temporaries."""

from __future__ import annotations

import os
from pathlib import Path


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
    targetless = data_dir / ".missing.parquet.0123456789abcdef0123456789abcdef.tmp"
    targetless.write_bytes(b"targetless")

    os.utime(stale, ns=(target.stat().st_mtime_ns - 2, target.stat().st_mtime_ns - 2))
    os.utime(newer, ns=(target.stat().st_mtime_ns + 2, target.stat().st_mtime_ns + 2))

    removed = cleanup_stale_owned_temps(data_root)

    assert removed == (stale,)
    assert not stale.exists()
    assert newer.is_file()
    assert arbitrary.is_file()
    assert targetless.is_file()
