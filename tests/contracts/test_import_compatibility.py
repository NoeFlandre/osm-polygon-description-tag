"""Legacy runtime imports remain identity-compatible with canonical modules."""

from osm_polygon_description_tag import _logging as legacy_logging
from osm_polygon_description_tag import _resources as legacy_resources
from osm_polygon_description_tag import config as legacy_config
from osm_polygon_description_tag.runtime import config as runtime_config
from osm_polygon_description_tag.runtime import logging as runtime_logging
from osm_polygon_description_tag.runtime import resources as runtime_resources


def test_legacy_config_exports_canonical_objects() -> None:
    assert legacy_config.Paths is runtime_config.Paths
    assert legacy_config.UnsafePathError is runtime_config.UnsafePathError


def test_legacy_logging_exports_canonical_objects() -> None:
    assert legacy_logging.RunLogger is runtime_logging.RunLogger


def test_legacy_resources_export_canonical_objects() -> None:
    assert legacy_resources.osmium_export_config is runtime_resources.osmium_export_config
    assert legacy_resources.dataset_card_template is runtime_resources.dataset_card_template
