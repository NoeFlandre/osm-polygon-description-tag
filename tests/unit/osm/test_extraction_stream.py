import subprocess
import sys
from pathlib import Path

import pytest

from osm_polygon_description_tag.osm.extraction import (
    STDERR_CAP_BYTES,
    OsmiumExportError,
    osmium_version,
    stream_export,
)

REC = b"0103\tway\t1\t1\t1\t2026-01-01T00:00:00Z\t{}\n"
REC2 = b"0103\tway\t2\t1\t1\t2026-01-01T00:00:00Z\t{}\n"


def _fake_osmium(
    tmp_path: Path,
    *,
    stdout: bytes = b"",
    stderr: bytes = b"",
    exit_code: int = 0,
    loop: bool = False,
) -> Path:
    program = tmp_path / "fake-osmium.py"
    lines = [f"#!{sys.executable}", "import sys"]
    if loop:
        lines += [
            "import time",
            "while True:",
            f"    sys.stdout.buffer.write({stdout!r})",
            "    sys.stdout.buffer.flush()",
            "    time.sleep(0.01)",
        ]
    else:
        lines.append(f"sys.stdout.buffer.write({stdout!r})")
    lines.append(f"sys.stderr.buffer.write({stderr!r})")
    lines.append(f"sys.exit({exit_code})")
    program.write_text("\n".join(lines) + "\n", encoding="utf-8")
    program.chmod(0o755)
    return program


def test_stream_export_yields_records_on_success(tmp_path: Path) -> None:
    exe = _fake_osmium(tmp_path, stdout=REC + REC2, exit_code=0)
    records = list(
        stream_export(Path("ignored.osm.pbf"), Path("ignored.json"), executable=str(exe))
    )

    assert [r.osm_id for r in records] == [1, 2]


def test_stream_export_raises_on_nonzero_exit(tmp_path: Path) -> None:
    exe = _fake_osmium(tmp_path, stdout=REC, stderr=b"osmium assembly error\n", exit_code=2)
    with pytest.raises(OsmiumExportError, match="exited 2"):
        list(stream_export(Path("x.osm.pbf"), Path("c.json"), executable=str(exe)))


def test_stream_export_wraps_missing_binary(tmp_path: Path) -> None:
    with pytest.raises(OsmiumExportError, match="not found"):
        list(
            stream_export(
                Path("x.osm.pbf"),
                Path("c.json"),
                executable=str(tmp_path / "does-not-exist"),
            )
        )


def test_stream_export_terminates_child_on_consumer_cancellation(tmp_path: Path) -> None:
    exe = _fake_osmium(tmp_path, stdout=REC, loop=True)
    gen = stream_export(Path("x.osm.pbf"), Path("c.json"), executable=str(exe))
    first = next(gen)
    assert first.osm_id == 1
    gen.close()  # consumer stops early; must not hang and must kill the child


def test_stream_export_bounds_retained_stderr(tmp_path: Path) -> None:
    flood = b"X" * (STDERR_CAP_BYTES + 1024 * 1024)
    exe = _fake_osmium(tmp_path, stdout=REC, stderr=flood, exit_code=1)
    with pytest.raises(OsmiumExportError) as info:
        list(stream_export(Path("x.osm.pbf"), Path("c.json"), executable=str(exe)))
    assert len(info.value.stderr) <= STDERR_CAP_BYTES
    assert info.value.stderr.endswith(b"X") or info.value.stderr.startswith(b"X")


def test_osmium_version_returns_first_stdout_line(tmp_path: Path) -> None:
    exe = tmp_path / "ver.py"
    exe.write_text(
        f"#!{sys.executable}\nimport sys\nsys.stdout.write('osmium version 1.16.0\\n')\n", "utf-8"
    )
    exe.chmod(0o755)
    assert osmium_version(executable=str(exe)) == "osmium version 1.16.0"


def test_osmium_version_raises_on_missing_binary() -> None:
    with pytest.raises((OsmiumExportError, FileNotFoundError, subprocess.SubprocessError)):
        osmium_version(executable="/does/not/exist-osmium")
