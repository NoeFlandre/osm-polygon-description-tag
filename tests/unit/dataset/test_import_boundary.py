"""Startup-cost contracts for lightweight dataset submodules."""

from __future__ import annotations

import importlib
import os
import subprocess
import sys
from pathlib import Path

import pytest


def test_language_checkpoint_import_does_not_start_plotting_stack(tmp_path: Path) -> None:
    """Language workers must not import Matplotlib just to read checkpoints."""
    env = os.environ.copy()
    env["MPLCONFIGDIR"] = str(tmp_path / "matplotlib")
    result = subprocess.run(  # noqa: S603 - executable and arguments are test-owned
        [
            sys.executable,
            "-c",
            (
                "import sys; "
                "from osm_polygon_description_tag.dataset.languages import checkpoint; "
                "assert 'matplotlib' not in sys.modules; "
                "assert checkpoint.CHECKPOINT_SCHEMA_VERSION == 1"
            ),
        ],
        check=True,
        env=env,
        capture_output=True,
        text=True,
        timeout=10,
    )
    assert result.stderr == ""


LAZY_PACKAGES = [
    "osm_polygon_description_tag.dataset",
    "osm_polygon_description_tag.dataset.languages",
]


@pytest.mark.parametrize("package_name", LAZY_PACKAGES)
def test_every_exported_name_resolves_to_its_defining_module(package_name: str) -> None:
    """Lazy resolution must hand back the very object a direct import gives.

    A lazy package is only safe if it is indistinguishable from an eager one,
    so each export is compared by identity against its defining module.
    """
    package = importlib.import_module(package_name)

    for name in package.__all__:
        module_name, attribute_name = package._LAZY_EXPORTS[name]
        expected = getattr(importlib.import_module(module_name), attribute_name)
        assert getattr(package, name) is expected, name


@pytest.mark.parametrize("package_name", LAZY_PACKAGES)
def test_the_export_table_and_public_surface_agree_exactly(package_name: str) -> None:
    """A name in one and not the other is either dead or unreachable."""
    package = importlib.import_module(package_name)

    assert sorted(package.__all__) == sorted(package._LAZY_EXPORTS)


@pytest.mark.parametrize("package_name", LAZY_PACKAGES)
def test_an_unknown_attribute_is_refused_by_its_exact_name(package_name: str) -> None:
    package = importlib.import_module(package_name)

    with pytest.raises(AttributeError) as error:
        package.__getattr__("definitely_not_exported")

    assert str(error.value) == (
        f"module {package_name!r} has no attribute 'definitely_not_exported'"
    )


@pytest.mark.parametrize("package_name", LAZY_PACKAGES)
def test_a_resolved_name_is_cached_so_the_import_happens_once(package_name: str) -> None:
    """Without the cache every attribute access re-enters the import machinery.

    That is the whole point of the indirection: resolve once, then behave like
    an ordinary module attribute.
    """
    package = importlib.import_module(package_name)
    name = sorted(package.__all__)[0]
    vars(package).pop(name, None)

    resolved = package.__getattr__(name)

    assert vars(package)[name] is resolved
