"""Strict JSON payload decoding for durable language-run state.

Persisted state is validated rather than coerced: ``int("3")``-style widening
would let a malformed or tampered payload rehydrate into an object that looks
structurally valid, so every reader here rejects the wrong type outright.
"""

from collections.abc import Mapping
from typing import cast


class PayloadReader:
    """Read required fields of one JSON object, raising a caller-chosen error."""

    __slots__ = ("_error", "_label", "_payload")

    def __init__(
        self,
        payload: Mapping[str, object],
        *,
        error: type[Exception],
        label: str,
    ) -> None:
        self._payload = payload
        self._error = error
        self._label = label

    def raw(self, key: str) -> object:
        """Return one present field without constraining its type."""
        if key not in self._payload:
            raise self._error(f"{self._label} payload is missing {key}")
        return self._payload[key]

    def has(self, key: str) -> bool:
        """Return whether an optional field is present in this payload."""
        return key in self._payload

    def text(self, key: str) -> str:
        """Return a string field, rejecting every other type."""
        value = self.raw(key)
        if not isinstance(value, str):
            raise self._error(f"{self._label} field {key} must be a string")
        return value

    def integer(self, key: str) -> int:
        """Return an integer field, rejecting booleans and floats."""
        value = self.raw(key)
        if type(value) is not int:
            raise self._error(f"{self._label} field {key} must be an integer")
        return value

    def number(self, key: str) -> float:
        """Return a real-number field, rejecting booleans."""
        value = self.raw(key)
        if isinstance(value, bool) or not isinstance(value, int | float):
            raise self._error(f"{self._label} field {key} must be a real number")
        return float(value)

    def items(self, key: str) -> list[object]:
        """Return a list field, rejecting every other type."""
        value = self.raw(key)
        if not isinstance(value, list):
            raise self._error(f"{self._label} field {key} must be a list")
        return cast(list[object], value)  # pragma: no mutate - static narrowing

    def texts(self, key: str) -> tuple[str, ...]:
        """Return a list field whose items are all strings."""
        values = self.items(key)
        for item in values:
            if not isinstance(item, str):
                raise self._error(f"{self._label} field {key} must contain only strings")
        typed_values = cast(list[str], values)  # pragma: no mutate - static narrowing
        return tuple(typed_values)

    def mapping(self, key: str) -> Mapping[str, object]:
        """Return an object field, rejecting every other type."""
        value = self.raw(key)
        if not isinstance(value, Mapping):
            raise self._error(f"{self._label} field {key} must be an object")
        return cast(Mapping[str, object], value)  # pragma: no mutate - static narrowing

    def reader(self, key: str) -> "PayloadReader":
        """Return a reader for one nested object field."""
        return PayloadReader(self.mapping(key), error=self._error, label=self._label)


def require_object(
    payload: object,
    *,
    error: type[Exception],
    label: str,
) -> PayloadReader:
    """Return a reader for ``payload``, rejecting non-object documents."""
    if not isinstance(payload, Mapping):
        raise error(f"{label} payload must be an object")
    typed_payload = cast(Mapping[str, object], payload)  # pragma: no mutate - static narrowing
    return PayloadReader(typed_payload, error=error, label=label)


__all__ = ["PayloadReader", "require_object"]
