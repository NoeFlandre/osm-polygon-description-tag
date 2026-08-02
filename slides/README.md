# Project slide decks

Two audience-specific decks are authored in [Colloquium](https://github.com/natolambert/colloquium):

- `codebase.md` — engineering architecture, contracts, safety, testing, and operations;
- `dataset.md` — research-facing scope, contents, current statistics, plots, and limitations.

Both decks use the same restrained visual system: warm paper background, deep
green section breaks, Inter typography, short takeaway titles, and source notes
on every slide. The dataset deck uses the current published hero, H3 density
map, and area-distribution histogram copied into `slides/assets/` for a stable
local build.

## Build

```bash
uv tool install colloquium
./slides/build.sh
```

The output is written to `slides/build/codebase/codebase.html` and
`slides/build/dataset/dataset.html`. The build script fails on the first invalid
deck.

## Review

Render each deck to PNGs with the Colloquium build output, then inspect the
slides at full size and as a montage. Rebuild after any copy or layout change;
the source notes intentionally make factual claims traceable to the code,
documentation, and generated dataset artifacts.
