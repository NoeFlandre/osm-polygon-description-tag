"""Canonical runtime support APIs."""

from osm_polygon_description_tag.runtime.cleanup import cleanup_stale_owned_temps
from osm_polygon_description_tag.runtime.config import Paths, UnsafePathError
from osm_polygon_description_tag.runtime.logging import RunLogger, configure_rotation
from osm_polygon_description_tag.runtime.presentation import TerminalPresenter
from osm_polygon_description_tag.runtime.resources import (
    dataset_card_template,
    osmium_export_config,
    package_data_dir,
    project_code_revision,
    project_root,
    resource_path,
)
from osm_polygon_description_tag.runtime.time import utc_now_iso

__all__ = [
    "Paths",
    "RunLogger",
    "TerminalPresenter",
    "UnsafePathError",
    "cleanup_stale_owned_temps",
    "configure_rotation",
    "dataset_card_template",
    "osmium_export_config",
    "package_data_dir",
    "project_code_revision",
    "project_root",
    "resource_path",
    "utc_now_iso",
]
