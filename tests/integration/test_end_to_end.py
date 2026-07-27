import shutil
from pathlib import Path

import pytest


@pytest.mark.integration
def test_synthetic_end_to_end(tmp_path: Path) -> None:
    """Full pipeline against a synthetic OSM fixture.

    Skips when osmium is unavailable. Per the implementation contract, a skip
    caused by a missing osmium binary is **not** acceptance evidence for this
    test; the integration gate must be re-run with osmium installed.
    """
    if shutil.which("osmium") is None:
        pytest.skip("osmium executable is required for the synthetic end-to-end test")

    fixture = Path("tests/fixtures/descriptions.osm")
    assert fixture.is_file(), "synthetic fixture must be committed"

    source_root = tmp_path / "raw"
    source_root.mkdir()
    fixture_target = source_root / fixture.name.replace(".osm", "-latest.osm")
    fixture_target.write_bytes(fixture.read_bytes())
    assert fixture_target.is_file()

    # When osmium is available, a future approved phase would:
    # 1. Convert the fixture to .osm.pbf in source_root.
    # 2. Invoke the CLI build, validate, generate-card, and publish-plan flows.
    # 3. Assert the expected included/excluded identities, hole-sensitive area,
    #    exact suffix preservation, GeoParquet metadata, manifest checksums,
    #    and ODbL attribution in the generated card.
