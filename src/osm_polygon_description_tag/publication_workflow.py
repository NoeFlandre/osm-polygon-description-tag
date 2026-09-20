"""Language export and Hugging Face publication workflows."""

from __future__ import annotations

from pathlib import Path

from osm_polygon_description_tag.publication.language import (
    LanguagePublicationError,
    build_language_upload_plan,
    export_language_annotations,
    language_config_yaml,
    read_language_export,
    render_language_card_section,
)
from osm_polygon_description_tag.publication.language_hub import build_language_hub
from osm_polygon_description_tag.publication.language_upload import (
    PUBLICATION_STATE_FILENAME,
    publish_language_export,
)
from osm_polygon_description_tag.runtime.presentation import print_json


def handle_export(run_dir: Path, export_dir: Path, card_section: Path | None) -> None:
    """Export one complete run into the additive language-v1 tree."""
    export = export_language_annotations(run_dir, export_dir)
    if card_section is not None:
        card_section.parent.mkdir(parents=True, exist_ok=True)
        section = render_language_card_section(export)
        card_section.write_text(section, encoding="utf-8")  # pragma: no mutate - codec alias only
    print_json(
        {
            "export_dir": str(export_dir),
            "card_section": None if card_section is None else str(card_section),
            "config_yaml": language_config_yaml(),
            **export.to_payload(),
        }
    )


def handle_publish(
    export_dir: Path,
    repo: str,
    confirm_repo: str,
    baseline_revision: str | None,
    apply: bool,
) -> None:
    """Plan the additive publication, and perform it only behind the apply gate."""
    export = read_language_export(export_dir)
    plan = build_language_upload_plan(export, repo, confirm_repo=confirm_repo)
    hub = build_language_hub()
    if apply and baseline_revision is None:
        raise LanguagePublicationError(
            "applying a publication requires the baseline revision the plan was built against"
        )
    baseline = baseline_revision or hub.repo_revision(repo)
    outcome = publish_language_export(
        plan,
        hub,
        baseline_revision=baseline,
        apply=apply,
        state_path=export_dir / PUBLICATION_STATE_FILENAME,
    )
    print_json(
        {
            "repo_id": repo,
            "plan_identity_sha256": plan.identity_sha256,
            "planned_files": [item.relative_path for item in plan.files],
            "baseline_revision": baseline,
            **outcome.to_payload(),
        }
    )
