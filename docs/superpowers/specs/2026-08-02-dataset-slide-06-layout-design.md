# Dataset Slide 6 Layout Design

## Goal

Rebalance slide 6 of the dataset deck, “Descriptions are predominantly base text, with a multilingual tail,” so its content reads as one deliberate composition instead of a short block stacked against the top edge.

## Scope

- Modify `slides/dataset.md`, the canonical Colloquium source.
- Add a slide-scoped `metric-slide` class and scoped CSS in the deck frontmatter.
- Keep the title, metric values, wording, numbers, palette, typography, and source attribution unchanged.
- Regenerate the dataset HTML and preview artifacts under `slides/build/dataset/` without changing unrelated slides.

## Layout

The slide will use a compact centered composition:

1. The existing title remains the visual entry point.
2. Base and localized description metrics remain side by side in equal-width blocks.
3. Both blocks receive the same heading, number, and metadata rhythm.
4. The localized-suffix explanation moves below both metrics as a shared, constrained summary with a subtle top rule, removing the current height imbalance between columns.
5. The source note and deck footer remain in their existing positions.

The new `metric-slide` class will make the slide’s natural content block vertically centered by disabling the default flex growth of `.slide-content` only on this slide. The metric grid and summary styles will be scoped beneath that class so other slides retain their current layout.

## Validation

- Build the dataset deck with the installed `colloquium` CLI into a temporary output directory, then synchronize the generated HTML, assets, and previews into `slides/build/dataset/` without deleting the existing output directory wholesale.
- Inspect the regenerated `preview/slide-06.png` at full size and compare neighboring slide previews for accidental layout changes.
- Confirm the rendered slide still contains the original factual copy and source note.
- Review `git diff` and repository status; do not modify unrelated source files.
