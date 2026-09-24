"""Small value validators shared across packages, parametrized by error class."""

import re

FINGERPRINT_PATTERN = re.compile(r"[0-9a-f]{64}\Z")


def validate_fingerprint(value: object, label: str, *, error: type[Exception]) -> None:
    """Raise ``error`` unless ``value`` is a lowercase SHA-256 hex digest."""
    if not isinstance(value, str) or FINGERPRINT_PATTERN.fullmatch(value) is None:
        raise error(f"{label} must be a lowercase SHA-256 hex fingerprint")


def required_text(value: object, label: str, *, error: type[Exception]) -> str:
    """Return ``value`` when it is a non-empty string, otherwise raise ``error``."""
    if not isinstance(value, str) or not value:
        raise error(f"{label} must be a non-empty string")
    return value


__all__ = ["FINGERPRINT_PATTERN", "required_text", "validate_fingerprint"]
