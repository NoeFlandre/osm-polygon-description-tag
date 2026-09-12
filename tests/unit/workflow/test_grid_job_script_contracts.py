"""Exact text of the script one Grid'5000 compute node actually runs.

Nothing re-reads this script before it executes, and a shard is processed once,
so every value baked into it is a contract: the scratch directory template, the
batch size, and whether a GlotLID model path is appended at all. A rendering
that silently loses a continuation or interpolates ``None`` would fail on the
node, hours after submission.
"""

from __future__ import annotations

import pytest

from osm_polygon_description_tag.workflow.grid_operator import (
    JobBundle,
    render_job_script,
)

_REMOTE = {
    "remote_project_dir": "/home/user/bundle/project",
    "remote_source_dir": "/home/user/bundle/source",
    "remote_run_dir": "/home/user/bundle/run",
}


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


def test_the_scratch_directory_template_names_this_bundle(tmp_path: object) -> None:
    """The template is passed to ``mktemp -d``; the six X's are what it replaces."""
    bundle = _bundle()

    script = render_job_script(bundle, **_REMOTE)

    assert (
        'job_tmp_root="$(mktemp -d "${TMPDIR:-/tmp}/'
        f'osm-polygon-description-tag-{bundle.bundle_id[:16]}.XXXXXX")"'
    ) in script


def test_the_default_batch_size_is_the_documented_five_hundred_and_twelve() -> None:
    """This default decides how many rows one commit covers on the node."""
    script = render_job_script(_bundle(), **_REMOTE)

    assert "  --batch-size 512 \\\n" in script


def test_a_lingua_job_appends_no_glotlid_option_at_all() -> None:
    """An empty option must contribute nothing, not the word ``None``."""
    script = render_job_script(_bundle(), processing_seconds=900, **_REMOTE)

    assert script.endswith(
        "  --shard region.parquet \\\n"
        "  --batch-size 512 \\\n"
        "  --budget-seconds 900\n"
        "\n"
        "uv run --no-sync osm-polygon-description-tag language validate \\\n"
        "  --run-dir /home/user/bundle/run \\\n"
        "  --shard region.parquet\n"
    )
    assert "glotlid" not in script


def test_a_cascade_job_appends_the_model_path_on_its_own_continued_line() -> None:
    """The option is appended to a line that must be continued with a backslash."""
    script = render_job_script(
        _bundle(),
        processing_seconds=900,
        glotlid_model_path="/home/user/models/model_v3.bin",
        **_REMOTE,
    )

    assert script.endswith(
        "  --shard region.parquet \\\n"
        "  --batch-size 512 \\\n"
        "  --budget-seconds 900 \\\n"
        "  --glotlid-model-path /home/user/models/model_v3.bin\n"
        "\n"
        "uv run --no-sync osm-polygon-description-tag language validate \\\n"
        "  --run-dir /home/user/bundle/run \\\n"
        "  --shard region.parquet\n"
    )


@pytest.mark.parametrize("batch_size", [1, 512, 4096])
def test_the_requested_batch_size_reaches_the_script_verbatim(batch_size: int) -> None:
    script = render_job_script(_bundle(), batch_size=batch_size, **_REMOTE)

    assert f"  --batch-size {batch_size} \\\n" in script
