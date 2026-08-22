"""Approved path defaults and immutable raw-source containment."""

from dataclasses import dataclass
from pathlib import Path

DEFAULT_SOURCE_ROOT = Path("/Volumes/Seagate M3/projects/osm-polygon-wikidata-only/raw")
DEFAULT_DATA_ROOT = Path("/Volumes/Seagate M3/projects/osm-polygon-description-tag")


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
    def defaults(cls) -> "Paths":
        return cls(DEFAULT_SOURCE_ROOT, DEFAULT_DATA_ROOT)

    def validate(self) -> "Paths":
        if _is_within(self.data_root, self.source_root):
            raise UnsafePathError(f"data root is inside immutable source: {self.data_root}")
        if _is_within(self.source_root, self.data_root):
            raise UnsafePathError(f"source root is inside data root: {self.source_root}")
        if self.source_root.resolve(strict=False) == self.data_root.resolve(strict=False):
            raise UnsafePathError("source root and data root must differ")
        return self
