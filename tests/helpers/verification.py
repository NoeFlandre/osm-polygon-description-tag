"""Small artifact builders for publication verification tests."""

import hashlib

from osm_polygon_description_tag.publication.models import UploadItem


def upload_item(path: str, content: bytes) -> UploadItem:
    """Describe a byte string with the path/size/digest tuple verifiers consume."""
    return UploadItem(
        relative_path=path,
        size_bytes=len(content),
        sha256=hashlib.sha256(content).hexdigest(),
    )
