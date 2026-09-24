"""Package-level exports stay identical to their canonical modules."""

import importlib

import pytest

from osm_polygon_description_tag import dataset as dataset_package
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
from osm_polygon_description_tag.workflow import OrchestratorError

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


def test_orchestrator_error_is_importable_from_the_workflow_package() -> None:
    assert OrchestratorError.__name__ == "OrchestratorError"
    assert OrchestratorError.__module__ == "osm_polygon_description_tag.workflow.source_runner"
