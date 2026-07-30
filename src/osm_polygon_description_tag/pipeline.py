"""Compatibility imports; canonical APIs live in `.workflow.build`."""

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
