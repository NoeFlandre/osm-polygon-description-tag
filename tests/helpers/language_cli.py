"""Reusable synthetic inputs for language and Grid CLI tests."""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path

import pytest
from shapely.geometry import Polygon

from osm_polygon_description_tag import (
    grid_transport,
    grid_workflow,
    language_workflow,
)
from osm_polygon_description_tag.cli import run
from osm_polygon_description_tag.dataset.languages.detector import LanguageDetector
from osm_polygon_description_tag.dataset.languages.models import (
    DEFAULT_LANGUAGE_SCOPE,
    LanguagePolicy,
    cascade_model_identity,
    language_model_identity,
)
from osm_polygon_description_tag.dataset.storage import write_geoparquet
from tests.conftest import make_record_dict
from tests.helpers.patching import patch_modules
from tests.helpers.sentences import fake_splitter

SAT_MODEL_PATH = "/models/sat-3l-sm/model.safetensors"
SHARD = "region.parquet"
NIGHT_INSTANT = datetime(2026, 9, 8, 20, 0, tzinfo=UTC)


@pytest.fixture(name="_fake_sentence_splitter", autouse=True)
def _fake_sentence_splitter(monkeypatch: pytest.MonkeyPatch) -> None:
    """Keep CLI tests about wiring; the real adapter has separate tests."""
    monkeypatch.setattr(
        language_workflow, "build_sat_splitter", lambda *, model_dir: fake_splitter()
    )


@pytest.fixture(name="night_clock")
def night_clock(monkeypatch: pytest.MonkeyPatch) -> datetime:
    """Freeze Grid handlers so their day/night policy is deterministic."""
    patch_modules(
        monkeypatch,
        (
            (grid_transport, "utc_now"),
            (grid_workflow, "utc_now"),
        ),
        lambda: NIGHT_INSTANT,
    )
    return NIGHT_INSTANT


def _confidence_values(text: str) -> dict[str, float]:
    if text.startswith("Le "):
        return {"fra": 0.95, "eng": 0.05}
    return {"eng": 0.9, "fra": 0.1}


@pytest.fixture(name="stub_detector")
def stub_detector(monkeypatch: pytest.MonkeyPatch) -> list[LanguagePolicy]:
    """Replace model construction with the deterministic synthetic detector."""
    policies: list[LanguagePolicy] = []

    def build(
        policy: LanguagePolicy,
        *,
        language_codes: tuple[str, ...] | None = None,
        glotlid_model_path: Path | None = None,
    ) -> object:
        policies.append(policy)
        identity = language_model_identity(
            policy, language_scope=language_codes or DEFAULT_LANGUAGE_SCOPE
        )
        return LanguageDetector(_confidence_values, policy=policy, identity=identity)

    def build_cascade(
        policy: LanguagePolicy,
        *,
        language_codes: tuple[str, ...] | None = None,
        glotlid_model_path: Path | None = None,
    ) -> object:
        build(policy, language_codes=language_codes, glotlid_model_path=glotlid_model_path)
        return LanguageDetector(
            _confidence_values,
            policy=policy,
            identity=cascade_model_identity(
                policy, language_scope=language_codes or DEFAULT_LANGUAGE_SCOPE
            ),
        )

    monkeypatch.setattr(language_workflow, "build_lingua_detector", build)
    monkeypatch.setattr(language_workflow, "build_language_detector", build_cascade)
    return policies


@pytest.fixture(name="project")
def project(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Create the same small fingerprintable project used by CLI tests."""
    root = tmp_path / "project"
    (root / "src").mkdir(parents=True)
    (root / "src" / "example.py").write_text("VALUE = 1\n", encoding="utf-8")
    (root / "pyproject.toml").write_text(
        "[project]\nname = 'synthetic-language-project'\nversion = '0.0.0'\n",
        encoding="utf-8",
    )
    (root / "uv.lock").write_text("lock = 1\n", encoding="utf-8")
    monkeypatch.chdir(root)
    return root


@pytest.fixture(name="source")
def source(tmp_path: Path) -> Path:
    """Create the fixed six-row bilingual shard used in CLI tests."""
    root = tmp_path / "source"
    records = (
        make_record_dict(
            Polygon([(0, 0), (0, 1), (1, 1), (1, 0)]),
            {
                "description": f"A synthetic description {index}",
                "description:fr": f"Le mur {index}",
            },
            osm_id=index + 1,
        )
        for index in range(6)
    )
    root.mkdir(parents=True)
    write_geoparquet(records, root / SHARD, batch_size=3)
    return root


def _stdout(capsys: pytest.CaptureFixture[str]) -> dict[str, object]:
    captured = capsys.readouterr()
    assert captured.err == ""
    return json.loads(captured.out)


def _prepare_run(tmp_path: Path, project: Path, source: Path) -> Path:
    run_dir = tmp_path / "run"
    assert (
        run(
            [
                "language",
                "prepare",
                "--source-root",
                str(source),
                "--run-dir",
                str(run_dir),
                "--project-root",
                str(project),
            ]
        )
        == 0
    )
    return run_dir
