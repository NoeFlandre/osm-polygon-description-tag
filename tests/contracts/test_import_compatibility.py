"""Legacy runtime imports remain identity-compatible with canonical modules."""

from types import ModuleType

from osm_polygon_description_tag import _logging as legacy_logging
from osm_polygon_description_tag import _resources as legacy_resources
from osm_polygon_description_tag import config as legacy_config
from osm_polygon_description_tag import dataset as dataset_package
from osm_polygon_description_tag import discovery as legacy_discovery
from osm_polygon_description_tag import extraction as legacy_extraction
from osm_polygon_description_tag import manifest as legacy_manifest
from osm_polygon_description_tag import orchestrator as legacy_orchestrator
from osm_polygon_description_tag import pipeline as legacy_pipeline
from osm_polygon_description_tag import publication as legacy_publication
from osm_polygon_description_tag import reporting as legacy_reporting
from osm_polygon_description_tag import schema as legacy_schema
from osm_polygon_description_tag import storage as legacy_storage
from osm_polygon_description_tag import transform as legacy_transform
from osm_polygon_description_tag.dataset import deduplication as dataset_deduplication
from osm_polygon_description_tag.dataset import manifest as dataset_manifest
from osm_polygon_description_tag.dataset import migration as dataset_migration
from osm_polygon_description_tag.dataset import reporting as dataset_reporting
from osm_polygon_description_tag.dataset import schema as dataset_schema
from osm_polygon_description_tag.dataset import storage as dataset_storage
from osm_polygon_description_tag.dataset import transform as dataset_transform
from osm_polygon_description_tag.orchestrator import OrchestratorError
from osm_polygon_description_tag.osm import discovery as osm_discovery
from osm_polygon_description_tag.osm import extraction as osm_extraction
from osm_polygon_description_tag.publication import models as publication_models
from osm_polygon_description_tag.publication import planning as publication_planning
from osm_polygon_description_tag.publication import upload as publication_upload
from osm_polygon_description_tag.runtime import config as runtime_config
from osm_polygon_description_tag.runtime import logging as runtime_logging
from osm_polygon_description_tag.runtime import resources as runtime_resources
from osm_polygon_description_tag.workflow import (
    BuildResult,
    OrchestrationReport,
    build_all,
    build_one,
    run_and_publish,
)


def test_legacy_config_exports_canonical_objects() -> None:
    assert legacy_config.Paths is runtime_config.Paths
    assert legacy_config.UnsafePathError is runtime_config.UnsafePathError


def test_legacy_logging_exports_canonical_objects() -> None:
    assert legacy_logging.RunLogger is runtime_logging.RunLogger


def test_legacy_resources_export_canonical_objects() -> None:
    assert legacy_resources.osmium_export_config is runtime_resources.osmium_export_config
    assert legacy_resources.dataset_card_template is runtime_resources.dataset_card_template


def test_legacy_osm_exports_canonical_objects() -> None:
    assert legacy_discovery.Source is osm_discovery.Source
    assert legacy_discovery.discover_sources is osm_discovery.discover_sources
    assert legacy_extraction.ExportRecord is osm_extraction.ExportRecord
    assert legacy_extraction.stream_export is osm_extraction.stream_export


def _assert_exact_compatibility_exports(legacy: ModuleType, canonical: ModuleType) -> None:
    assert legacy.__all__ == canonical.__all__
    for name in legacy.__all__:
        assert getattr(legacy, name) is getattr(canonical, name)


def test_legacy_schema_exports_all_canonical_objects() -> None:
    _assert_exact_compatibility_exports(legacy_schema, dataset_schema)


def test_legacy_transform_exports_all_canonical_objects() -> None:
    _assert_exact_compatibility_exports(legacy_transform, dataset_transform)


def test_legacy_storage_exports_all_canonical_objects() -> None:
    _assert_exact_compatibility_exports(legacy_storage, dataset_storage)


def test_legacy_manifest_exports_all_canonical_objects() -> None:
    _assert_exact_compatibility_exports(legacy_manifest, dataset_manifest)


def test_legacy_reporting_exports_all_canonical_objects() -> None:
    _assert_exact_compatibility_exports(legacy_reporting, dataset_reporting)


def test_dataset_package_exports_exact_stable_module_api() -> None:
    canonical_modules = (
        dataset_schema,
        dataset_transform,
        dataset_storage,
        dataset_manifest,
        dataset_migration,
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


def test_orchestrator_error_preserves_public_class_identity() -> None:
    assert OrchestratorError.__name__ == "OrchestratorError"
    assert OrchestratorError.__module__ == "osm_polygon_description_tag.orchestrator"


def test_workflow_legacy_imports_are_identical() -> None:
    assert legacy_pipeline.BuildResult is BuildResult
    assert legacy_pipeline.build_one is build_one
    assert legacy_pipeline.build_all is build_all
    assert legacy_orchestrator.OrchestrationReport is OrchestrationReport
    assert legacy_orchestrator.run_and_publish is run_and_publish
