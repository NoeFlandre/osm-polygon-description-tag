"""Build public GeoParquet for described OpenStreetMap polygons."""

from importlib.metadata import PackageNotFoundError, version

try:
    # pyproject.toml is the single source of the version (#73).
    __version__ = version("osm-polygon-description-tag")
except PackageNotFoundError:  # pragma: no cover - only when run from an uninstalled tree
    __version__ = "0+unknown"
