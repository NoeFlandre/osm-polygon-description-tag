"""Compatibility imports for :mod:`osm_polygon_description_tag.workflow.build`."""

from osm_polygon_description_tag.workflow.build import (
    BuildResult,
    PipelineError,
    build_all,
    build_one,
    safe_osmium_version,
)

__all__ = [
    "BuildResult",
    "PipelineError",
    "build_all",
    "build_one",
    "safe_osmium_version",
]
