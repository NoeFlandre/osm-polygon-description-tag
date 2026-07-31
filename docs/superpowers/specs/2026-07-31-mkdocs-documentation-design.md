# MkDocs Documentation Design

## Goal

Make the project documentation concise, public-facing, navigable, and
accurate through a strict MkDocs Material site while keeping the root README
as a short repository landing page.

## Site structure

The site uses these public pages:

- **Home:** what the dataset contains, why it exists, and the core guarantees.
- **Getting started:** uv setup, osmium prerequisite, safe inspection, and the
  single stoppable/resumable production command.
- **Dataset contract:** inclusion rules, full geometry, area, tags, names,
  descriptions, word statistics, schema, and limitations.
- **Operations:** Seagate storage boundaries, logs, checkpoints, preflight,
  interruption, resume behavior, and Hugging Face publication safety.
- **CLI reference:** every public command with purpose, inputs, outputs, and
  side effects.
- **Development:** uv, Ruff, ty, pytest, pre-commit, Just, package layout,
  and CI gates.
- **Architecture:** one-way package dependencies and end-to-end data flow.

Historical design and plan documents remain in the repository but are excluded
from the public navigation and strict-build warning surface.

## Accuracy rules

- No hand-written dataset counts or freshness claims appear in the site.
- The generated Hugging Face card remains the artifact-derived source of live
  dataset statistics; the site explains the contract and links to it.
- The immutable raw PBF root and Seagate generated-data root are stated
  explicitly.
- Every operational command is copied from the tested CLI/Just contract.
- Documentation never implies that tests, MkDocs builds, or CI access real PBFs
  or publish to Hugging Face.

## Tooling

`mkdocs-material` is a development dependency. `mkdocs.yml` enables strict
builds, built-in search, navigation tabs, code copy buttons, and Markdown
extensions already compatible with the repository. GitHub Actions builds the
site as part of the quality workflow; publication remains a separate action.
