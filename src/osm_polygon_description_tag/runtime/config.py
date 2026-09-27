"""Source/data root resolution and immutable raw-source containment."""

import os
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path

SOURCE_ROOT_ENV = "OSM_POLYGON_SOURCE_ROOT"
DATA_ROOT_ENV = "OSM_POLYGON_DATA_ROOT"


class MissingPathError(ValueError):
    """Raised when a root is given neither as a CLI option nor in the environment."""


class UnsafePathError(ValueError):
    """Raised when a configured path violates an approved trust boundary."""


def _is_within(child: Path, parent: Path) -> bool:
    # pragma: no mutate start - strict=False and None are equivalent here
    resolved_child = child.resolve(strict=False)
    resolved_parent = parent.resolve(strict=False)
    # pragma: no mutate end
    return resolved_child.is_relative_to(resolved_parent)


@dataclass(frozen=True)
class Paths:
    source_root: Path
    data_root: Path

    @classmethod
    def resolve(
        cls,
        source_root: Path | None,
        data_root: Path | None,
        env: Mapping[str, str] | None = None,
    ) -> "Paths":
        """Resolve both roots: CLI option, then environment, else a clear error.

        There is no machine-specific fallback. The result is validated.
        """
        environment = os.environ if env is None else env
        return cls(
            _resolve_root(source_root, environment, SOURCE_ROOT_ENV, "--source-root"),
            _resolve_root(data_root, environment, DATA_ROOT_ENV, "--data-root"),
        ).validate()

    def validate(self) -> "Paths":
        if _is_within(self.data_root, self.source_root):
            raise UnsafePathError(f"data root is inside immutable source: {self.data_root}")
        if _is_within(self.source_root, self.data_root):
            raise UnsafePathError(f"source root is inside data root: {self.source_root}")
        if self.source_root.resolve(strict=False) == self.data_root.resolve(strict=False):
            raise UnsafePathError("source root and data root must differ")
        return self


def _resolve_root(option: Path | None, env: Mapping[str, str], variable: str, flag: str) -> Path:
    if option is not None:
        return option
    value = env.get(variable, "").strip()
    if value:
        return Path(value)
    raise MissingPathError(f"no {flag} given and {variable} is not set; pass one of them")
