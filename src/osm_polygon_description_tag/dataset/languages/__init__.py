"""Pinned language detection and one-record-per-description contracts."""

from osm_polygon_description_tag.dataset.languages.detector import (
    LanguageDetectionCallable,
    LanguageDetectionError,
    LanguageDetector,
    LanguageLibraryVersionError,
    build_lingua_detector,
    compute_language_confidence_values,
    detect_description_entries,
    detect_multiple_languages_of,
)
from osm_polygon_description_tag.dataset.languages.models import (
    DEFAULT_LANGUAGE_POLICY,
    DEFAULT_LANGUAGE_SCOPE,
    LINGUA_LIBRARY_NAME,
    PINNED_LINGUA_VERSION,
    LanguageModelIdentity,
    LanguagePolicy,
    LanguageResult,
    LanguageStatus,
    language_model_identity,
)
from osm_polygon_description_tag.dataset.languages.records import (
    DescriptionEntry,
    DescriptionRecordError,
    extract_description_entries,
)

__all__ = [
    "DEFAULT_LANGUAGE_POLICY",
    "DEFAULT_LANGUAGE_SCOPE",
    "LINGUA_LIBRARY_NAME",
    "PINNED_LINGUA_VERSION",
    "DescriptionEntry",
    "DescriptionRecordError",
    "LanguageDetectionCallable",
    "LanguageDetectionError",
    "LanguageDetector",
    "LanguageLibraryVersionError",
    "LanguageModelIdentity",
    "LanguagePolicy",
    "LanguageResult",
    "LanguageStatus",
    "build_lingua_detector",
    "compute_language_confidence_values",
    "detect_description_entries",
    "detect_multiple_languages_of",
    "extract_description_entries",
    "language_model_identity",
]
