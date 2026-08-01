# Dataset Card Hero Image Design

## Goal

Use the newly supplied branding image as the first visible content in both the GitHub README and Hugging Face dataset card, while maintaining one canonical source file and preserving deterministic dataset publication.

## Canonical asset

Rename the root-level `PNG image.png` to `assets/dataset-card-hero.png`. This repository asset is the sole source of truth. The GitHub README references it directly, and dataset-card generation copies the exact bytes into `<data_root>/assets/dataset-card-hero.png`.

## Rendering

The repository `README.md` places the image before its title so it is the first rendered element on GitHub. The packaged and documentation-mirror dataset-card templates place `![OSM Polygon Description Tag dataset hero](assets/dataset-card-hero.png)` immediately after Hugging Face YAML frontmatter, which is the earliest valid Markdown position and therefore the top of the rendered dataset card.

## Generation and publication flow

Dataset documentation generation obtains the canonical packaged hero resource and atomically installs it in the data-root assets directory before writing the generated card. Publication validation permits and requires the hero alongside the existing map and histogram. Both per-PBF and metadata-only upload plans include the hero, ensuring initial, resumed, and metadata-only publications synchronize it to Hugging Face.

## Failure handling

Generation fails clearly if the packaged hero resource is unavailable or cannot be copied. Existing publication validation continues to reject unknown assets, temporary files, symlinks, and missing required assets. Upload identity includes the hero, so byte changes produce a new plan and are remotely verified by the existing publication mechanism.

## Verification

Update focused generation and publication tests to assert the hero is copied, referenced, required, allowlisted, and included in upload plans. Run the repository formatting, lint, type-check, test, package-build, and strict MkDocs checks. Regenerate the real dataset card, inspect its publication plan, publish and verify Hugging Face, then commit and push the repository changes to GitHub.
