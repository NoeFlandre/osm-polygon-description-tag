"""Pinned language detection and one-record-per-description contracts."""

from importlib import import_module

__all__ = [
    "CASCADE_DETECTOR_NAME",
    "DEFAULT_LANGUAGE_POLICY",
    "DEFAULT_LANGUAGE_SCOPE",
    "GLOTLID_DETECTOR_NAME",
    "GLOTLID_LIBRARY_NAME",
    "GLOTLID_LIBRARY_VERSION",
    "GLOTLID_MODEL_FILENAME",
    "GLOTLID_MODEL_REPOSITORY",
    "GLOTLID_MODEL_REVISION",
    "GLOTLID_MODEL_SHA256",
    "GLOTLID_RUNTIME_LIBRARY_NAME",
    "GLOTLID_RUNTIME_LIBRARY_VERSION",
    "LINGUA_DETECTOR_NAME",
    "LINGUA_LIBRARY_NAME",
    "PINNED_LINGUA_VERSION",
    "DescriptionEntry",
    "DescriptionRecordError",
    "FallbackLanguageDetector",
    "GlotLIDLabelError",
    "LanguageDetectionCallable",
    "LanguageDetectionError",
    "LanguageDetector",
    "LanguageLibraryVersionError",
    "LanguageModelIdentity",
    "LanguagePolicy",
    "LanguageResult",
    "LanguageStatus",
    "build_glotlid_detector",
    "build_language_detector",
    "build_lingua_detector",
    "cascade_model_identity",
    "compute_language_confidence_values",
    "detect_description_entries",
    "detect_multiple_languages_of",
    "extract_description_entries",
    "glotlid_model_identity",
    "language_model_identity",
]

_LAZY_EXPORTS = {
    **{
        name: ("osm_polygon_description_tag.dataset.languages.detector", name)
        for name in (
            "FallbackLanguageDetector",
            "LanguageDetectionCallable",
            "LanguageDetectionError",
            "LanguageDetector",
            "LanguageLibraryVersionError",
            "build_language_detector",
            "build_lingua_detector",
            "compute_language_confidence_values",
            "detect_description_entries",
            "detect_multiple_languages_of",
        )
    },
    "GlotLIDLabelError": (
        "osm_polygon_description_tag.dataset.languages.glotlid",
        "GlotLIDLabelError",
    ),
    "build_glotlid_detector": (
        "osm_polygon_description_tag.dataset.languages.glotlid",
        "build_glotlid_detector",
    ),
    **{
        name: ("osm_polygon_description_tag.dataset.languages.models", name)
        for name in (
            "CASCADE_DETECTOR_NAME",
            "DEFAULT_LANGUAGE_POLICY",
            "DEFAULT_LANGUAGE_SCOPE",
            "GLOTLID_DETECTOR_NAME",
            "GLOTLID_LIBRARY_NAME",
            "GLOTLID_LIBRARY_VERSION",
            "GLOTLID_MODEL_FILENAME",
            "GLOTLID_MODEL_REPOSITORY",
            "GLOTLID_MODEL_REVISION",
            "GLOTLID_MODEL_SHA256",
            "GLOTLID_RUNTIME_LIBRARY_NAME",
            "GLOTLID_RUNTIME_LIBRARY_VERSION",
            "LINGUA_DETECTOR_NAME",
            "LINGUA_LIBRARY_NAME",
            "PINNED_LINGUA_VERSION",
            "LanguageModelIdentity",
            "LanguagePolicy",
            "LanguageResult",
            "LanguageStatus",
            "cascade_model_identity",
            "glotlid_model_identity",
            "language_model_identity",
        )
    },
    **{
        name: ("osm_polygon_description_tag.dataset.languages.records", name)
        for name in ("DescriptionEntry", "DescriptionRecordError", "extract_description_entries")
    },
}


def __getattr__(name: str) -> object:
    """Load detector/model dependencies only when a caller uses them.

    Checkpoint, receipt, and path helpers are intentionally lightweight. Eagerly
    importing this package used to initialize Lingua, GlotLID, and Torch for
    every Grid operator subprocess, even when no text was being detected.
    """
    target = _LAZY_EXPORTS.get(name)
    if target is None:
        raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
    module_name, attribute_name = target
    value = getattr(import_module(module_name), attribute_name)
    globals()[name] = value
    return value
