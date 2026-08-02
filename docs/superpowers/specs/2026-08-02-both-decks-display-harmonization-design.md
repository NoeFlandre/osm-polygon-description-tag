# Dataset and Codebase Deck Display Harmonization Design

## Goal

Harmonize the visual disposition of every slide in the dataset and codebase decks while preserving all displayed content exactly: wording, numbers, code, links, images, and factual claims.

## Design

Use a shared CSS layout system in each deck's existing `custom_css` block:

- Center natural content blocks vertically on content slides instead of allowing the flex child to consume the entire slide height.
- Center section-break content and constrain its diagram width; use a light source-note color on the deep-green background.
- Give title slides a consistent roomy safe area and constrain title-slide code panels to their content width.
- Set two-column and three-column grids to top-align their cells, add restrained hairline dividers, and keep columns visually independent rather than stretching short cells.
- Standardize source-note clearance above the footer, code-panel spacing, callout spacing, and content max-width.
- Keep slide-family-specific treatments for metrics, plots, guardrails, tables, and code panels, using existing classes or non-rendered class comments only where a selector cannot be expressed safely.

The dataset slide 6 metric grid remains the canonical treatment for paired metrics. Supporting copy on other metric slides may be visually grouped beneath the metric row, but its text and reading order will remain unchanged.

## Validation

Build and capture all 11 dataset slides and all 12 codebase slides. Inspect every full-size preview and both montages for clipping, awkward wraps, inconsistent anchors, source-note/footer collisions, and unintended content changes. Compare rendered text against the original source and run the repository test suite.
