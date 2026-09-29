"""The CLI must not import matplotlib until a command renders (#84)."""

from __future__ import annotations

import subprocess
import sys


def test_importing_the_cli_does_not_import_matplotlib() -> None:
    code = (
        "import sys, osm_polygon_description_tag.cli; "
        "print(sorted(m for m in sys.modules if m.split('.')[0] == 'matplotlib'))"
    )
    completed = subprocess.run(  # noqa: S603
        [sys.executable, "-c", code], check=True, capture_output=True, text=True
    )

    assert completed.stdout.strip() == "[]"


def test_pyplot_helper_selects_the_agg_backend() -> None:
    from osm_polygon_description_tag.dataset.geography.mpl import pyplot

    assert pyplot().get_backend().lower() == "agg"
