# Harmonize Dataset Slide 6 Layout Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Vertically rebalance dataset slide 6 and give its two description metrics equal visual weight without changing the deck’s visual system or factual content.

**Architecture:** Keep `slides/dataset.md` as the single authored source. Add a slide-local `metric-slide` class and a small set of nested CSS rules in the existing `custom_css` block, then replace the asymmetric Markdown column body with an explicit two-column metric grid and a full-width summary. Regenerate the ignored HTML/PNG build artifacts in a temporary directory first, then copy only the generated dataset outputs into the requested build location.

**Tech Stack:** Colloquium Markdown, embedded CSS, installed `colloquium` CLI, PNG preview inspection.

---

### Task 1: Author the balanced metric composition

**Files:**
- Modify: `/Users/noeflandre/osm-polygon-description-tag/slides/dataset.md` (custom CSS and slide 6 body)

- [ ] **Step 1: Add scoped layout rules to `custom_css`**

Add these rules after the existing `.balanced` rule, keeping the current palette variables and typography:

```css
  .metric-slide {
    justify-content: center;
  }
  .metric-slide > .slide-content {
    flex: 0 0 auto;
  }
  .metric-slide h2 {
    margin-bottom: 1.3rem;
  }
  .metric-grid {
    display: grid;
    grid-template-columns: repeat(2, minmax(0, 1fr));
    gap: 4rem;
    align-items: start;
    width: 100%;
  }
  .metric-block {
    min-height: 8.5rem;
  }
  .metric-block + .metric-block {
    border-left: 1px solid var(--colloquium-border);
    padding-left: 4rem;
  }
  .metric-heading {
    margin-bottom: 0.5rem;
    font-size: 1.06rem;
    line-height: 1.42;
    font-weight: 700;
  }
  .metric-summary {
    grid-column: 1 / -1;
    max-width: 54rem;
    margin: 1.25rem auto 0;
    padding-top: 1rem;
    border-top: 1px solid var(--colloquium-border);
    color: var(--colloquium-muted);
    text-align: center;
  }
```

- [ ] **Step 2: Replace slide 6’s asymmetric columns with the metric grid**

Replace the slide’s `<!-- class: balanced -->` marker with `<!-- class: metric-slide -->`, remove the `<!-- columns: 2 -->` marker for that slide, and use this body while preserving the existing heading and source note exactly:

```html
<div class="metric-grid">
  <div class="metric-block">
    <div class="metric-heading">Base descriptions</div>
    <div class="big-number">887,077</div>
    <div class="metric-label">values · 5.11M words · median 4 words</div>
  </div>
  <div class="metric-block">
    <div class="metric-heading">Localized descriptions</div>
    <div class="big-number">32,049</div>
    <div class="metric-label">values · 214k words · median 3 words</div>
  </div>
  <p class="metric-summary">The most common exact localized suffixes are <code>de</code>, <code>en</code>, <code>it</code>, <code>fr</code>, and <code>ru</code>. Suffixes are preserved verbatim; they are not asserted to be valid language codes.</p>
</div>
```

Keep the existing source note immediately after the grid so it remains an absolutely positioned attribution.

- [ ] **Step 3: Review the source diff for scope and factual preservation**

Run:

```bash
git diff --check -- slides/dataset.md
git diff -- slides/dataset.md
```

Expected: only the slide-scoped CSS, class marker, and metric markup change; all five numeric facts and the source-note text remain present.

### Task 2: Rebuild the requested dataset output

**Files:**
- Modify: `/Users/noeflandre/osm-polygon-description-tag/slides/build/dataset/dataset.html`
- Modify: `/Users/noeflandre/osm-polygon-description-tag/slides/build/dataset/preview/slide-*.png`
- Modify: `/Users/noeflandre/osm-polygon-description-tag/slides/build/dataset/montage.png` if regenerated

- [ ] **Step 1: Build into an isolated temporary directory**

Run from the repository root:

```bash
tmp_dir="$(mktemp -d /tmp/osm-dataset-slide.XXXXXX)"
colloquium build slides/dataset.md -o "$tmp_dir/dataset"
cp -R slides/assets "$tmp_dir/dataset/assets"
printf '%s\n' "$tmp_dir/dataset"
```

Expected: Colloquium exits successfully and writes a new `dataset.html` plus preview assets without touching the existing build directory.

- [ ] **Step 2: Synchronize generated files without wholesale cleanup**

Copy the freshly built HTML, preview PNGs, montage (when present), and assets into `/Users/noeflandre/osm-polygon-description-tag/slides/build/dataset/`, overwriting only same-named generated artifacts and preserving unrelated files:

```bash
cp "$tmp_dir/dataset/dataset.html" slides/build/dataset/dataset.html
cp -R "$tmp_dir/dataset/preview/." slides/build/dataset/preview/
[ ! -f "$tmp_dir/dataset/montage.png" ] || cp "$tmp_dir/dataset/montage.png" slides/build/dataset/montage.png
cp -R "$tmp_dir/dataset/assets/." slides/build/dataset/assets/
```

### Task 3: Verify visual and textual output

**Files:**
- Inspect: `/Users/noeflandre/osm-polygon-description-tag/slides/build/dataset/preview/slide-06.png`
- Inspect: `/Users/noeflandre/osm-polygon-description-tag/slides/build/dataset/preview/slide-05.png`
- Inspect: `/Users/noeflandre/osm-polygon-description-tag/slides/build/dataset/preview/slide-07.png`

- [ ] **Step 1: Inspect slide 6 at full size**

Read `preview/slide-06.png` and confirm the title, equal metric blocks, centered summary, source note, and footer are all visible with comfortable whitespace. The metric content must no longer be confined to the upper quarter of the canvas.

- [ ] **Step 2: Confirm neighboring slides and rendered copy**

Read slides 5 and 7 and compare their geometry visually. Then run:

```bash
rg -n "Descriptions are predominantly|887,077|32,049|5.11M|214k|501|Source: generated stats.json" slides/build/dataset/dataset.html
```

Expected: neighboring slide previews remain unchanged, and slide 6 contains the original title, metrics, metadata, and attribution.

- [ ] **Step 3: Review final repository state**

Run:

```bash
git diff --check
git status --short
```

Expected: no whitespace errors; only the intended authored source is tracked as a code/documentation change, with generated build files shown only if the repository tracks them.
