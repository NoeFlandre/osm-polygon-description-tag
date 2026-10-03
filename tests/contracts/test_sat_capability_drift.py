"""Hermetic checks for the explicit cross-repository capability audit."""

from __future__ import annotations

import hashlib
import io
import json
from pathlib import Path

import pytest

from scripts import check_sat_capabilities as audit

REFERENCE = Path(__file__).resolve().parents[2] / audit.REFERENCES["description"][1]


def test_equal_references_report_all_consumers(capsys) -> None:
    content = REFERENCE.read_bytes()
    assert audit.compare_references(dict.fromkeys(audit.REFERENCES, content))
    output = capsys.readouterr().out
    for name in audit.REFERENCES:
        assert f"{name}: sat-3l-sm-v1 sha256:" in output


def test_changed_version_reports_drift_even_when_its_digest_is_valid() -> None:
    content = REFERENCE.read_bytes()
    changed = json.loads(content)
    changed.pop("digest")
    changed["reference_version"] = "sat-3l-sm-v2"
    canonical = json.dumps(changed, sort_keys=True, separators=(",", ":")).encode()
    changed["digest"] = "sha256:" + hashlib.sha256(canonical).hexdigest()
    assert not audit.compare_references(
        {"description": content, "website": json.dumps(changed).encode()}
    )


def test_corrupt_payload_is_rejected() -> None:
    with pytest.raises(ValueError, match="invalid capability payload digest"):
        audit.reference_identity(REFERENCE.read_bytes().replace(b'"en"', b'"xx"'))


def test_local_paths_need_no_network(tmp_path, monkeypatch) -> None:
    path = tmp_path / audit.REFERENCES["website"][1]
    path.parent.mkdir(parents=True)
    path.write_bytes(b"local reference")
    monkeypatch.setattr(audit, "urlopen", lambda *args, **kwargs: pytest.fail("network access"))
    assert audit.read_reference("website", tmp_path, "main") == b"local reference"


def test_remote_origin_ref_timeout_and_read_bound(monkeypatch) -> None:
    class Response(io.BytesIO):
        def read(self, size=-1):
            assert size == 65536
            return super().read(size)

    def open_reference(url, *, timeout):
        assert url == (
            "https://raw.githubusercontent.com/NoeFlandre/osm-polygon-wikidata-only/"
            "review%2Fshared/src/osm_polygon_wikidata_only/v2/sat-capabilities.json"
        )
        assert timeout == 30
        return Response(b"remote reference")

    monkeypatch.setattr(audit, "urlopen", open_reference)
    assert audit.read_reference("wikidata", None, "review/shared") == b"remote reference"


@pytest.mark.parametrize("equal", [True, False])
def test_cli_status_and_ref_selection(monkeypatch, capsys, equal) -> None:
    reads = []

    def read_reference(name, checkout, ref):
        reads.append((name, checkout, ref))
        return b"reference"

    def compare(references):
        assert references == dict.fromkeys(audit.REFERENCES, b"reference")
        return equal

    monkeypatch.setattr(audit, "read_reference", read_reference)
    monkeypatch.setattr(audit, "compare_references", compare)
    assert audit.main(["--description", ".", "--website-ref", "abc123"]) == (0 if equal else 1)
    assert reads == [
        ("description", Path(), "main"),
        ("website", None, "abc123"),
        ("wikidata", None, "main"),
    ]
    assert ("DRIFT:" in capsys.readouterr().out) is not equal


def test_missing_reference_is_a_failure(tmp_path) -> None:
    with pytest.raises(FileNotFoundError):
        audit.main(["--description", str(tmp_path)])
