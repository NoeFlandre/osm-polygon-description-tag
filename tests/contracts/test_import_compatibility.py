"""Package-level exports stay identical to their canonical modules."""

import importlib
from types import ModuleType

import pytest

from osm_polygon_description_tag import dataset as dataset_package
from osm_polygon_description_tag import language_cli
from osm_polygon_description_tag import publication as legacy_publication
from osm_polygon_description_tag.dataset import deduplication as dataset_deduplication
from osm_polygon_description_tag.dataset import manifest as dataset_manifest
from osm_polygon_description_tag.dataset import migration as dataset_migration
from osm_polygon_description_tag.dataset import reporting as dataset_reporting
from osm_polygon_description_tag.dataset import schema as dataset_schema
from osm_polygon_description_tag.dataset import storage as dataset_storage
from osm_polygon_description_tag.dataset import text_migration as dataset_text_migration
from osm_polygon_description_tag.dataset import transform as dataset_transform
from osm_polygon_description_tag.publication import models as publication_models
from osm_polygon_description_tag.publication import planning as publication_planning
from osm_polygon_description_tag.publication import upload as publication_upload
from osm_polygon_description_tag.workflow import OrchestratorError, grid_operator

REMOVED_TOP_LEVEL_SHIMS = (
    "config",
    "_logging",
    "_resources",
    "discovery",
    "extraction",
    "schema",
    "transform",
    "storage",
    "reporting",
    "manifest",
    "pipeline",
    "orchestrator",
)


@pytest.mark.parametrize("name", REMOVED_TOP_LEVEL_SHIMS)
def test_top_level_compatibility_shims_are_gone(name: str) -> None:
    with pytest.raises(ModuleNotFoundError):
        importlib.import_module(f"osm_polygon_description_tag.{name}")


def test_dataset_package_exports_exact_stable_module_api() -> None:
    canonical_modules = (
        dataset_schema,
        dataset_transform,
        dataset_storage,
        dataset_manifest,
        dataset_migration,
        dataset_text_migration,
        dataset_deduplication,
        dataset_reporting,
    )
    intended_package_exports = set().union(*(module.__all__ for module in canonical_modules))
    intended_package_exports -= {"GEOD", "utc_now_iso"}
    assert set(dataset_package.__all__) == intended_package_exports
    for name in dataset_package.__all__:
        defining_module = next(module for module in canonical_modules if name in module.__all__)
        assert getattr(dataset_package, name) is getattr(defining_module, name)


def test_publication_package_exports_canonical_objects() -> None:
    assert legacy_publication.UploadPlan is publication_models.UploadPlan
    assert legacy_publication.UploadItem is publication_models.UploadItem
    assert legacy_publication.PublicationError is publication_models.PublicationError
    assert legacy_publication.create_upload_plan is publication_planning.create_upload_plan
    assert legacy_publication.execute_upload is publication_upload.execute_upload


def test_publication_package_exposes_only_supported_names() -> None:
    assert all(not name.startswith("_") for name in legacy_publication.__all__)
    assert (
        legacy_publication.build_metadata_only_upload_plan
        is publication_planning.build_metadata_only_upload_plan
    )
    assert (
        legacy_publication.build_per_pbf_upload_plan
        is publication_planning.build_per_pbf_upload_plan
    )
    assert (
        legacy_publication.default_runner_with_retry is publication_upload.default_runner_with_retry
    )


def test_orchestrator_error_is_importable_from_the_workflow_package() -> None:
    assert OrchestratorError.__name__ == "OrchestratorError"
    assert OrchestratorError.__module__ == "osm_polygon_description_tag.workflow.source_runner"


@pytest.mark.parametrize(
    ("module", "module_name"),
    [
        (language_cli, "osm_polygon_description_tag.language_cli"),
        (grid_operator, "osm_polygon_description_tag.workflow.grid_operator"),
    ],
)
def test_compatibility_module_reports_unknown_attributes_exactly(
    module: ModuleType,
    module_name: str,
) -> None:
    with pytest.raises(
        AttributeError,
        match=rf"^module {module_name!r} has no attribute 'unknown_attribute'$",
    ):
        module.__getattr__("unknown_attribute")
