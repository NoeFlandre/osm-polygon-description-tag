# Project slide decks

Two decks are authored in [Colloquium](https://github.com/natolambert/colloquium). Each deck has a different audience:

- `codebase.md`: engineering architecture, contracts, safety, testing, and operations.
- `dataset.md`: research scope, contents, current statistics, plots, and limits.

Both decks use the same restrained visual system: a warm paper background, deep
green section breaks, Inter typography, short takeaway titles, and source notes
on every slide. The dataset deck uses the current published hero image, H3
density map, and area-distribution histogram. Copies of these images are in
`slides/assets/`, so the local build is stable.

## Build

```bash
uv tool install colloquium
./slides/build.sh
```

The build writes its output to `slides/build/codebase/codebase.html` and
`slides/build/dataset/dataset.html`. The build script stops at the first invalid
deck.

The MkDocs GitHub Pages site publishes the dataset deck at
`https://noeflandre.github.io/osm-polygon-description-tag/slides/dataset/dataset.html`.
The docs workflow rebuilds and deploys both decks on every push to `main`.

## Review

Render each deck to PNGs with the Colloquium build output. Then examine the
slides at full size and as a montage. Rebuild after each change to the copy or
the layout. The source notes make the factual claims traceable to the code, the
documentation, and the generated dataset artifacts. This is intentional.
