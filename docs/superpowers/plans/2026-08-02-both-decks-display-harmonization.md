# Harmonize Both Slide Decks Display Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make every dataset and codebase slide visually balanced and presentation-ready without changing any displayed content.

**Architecture:** Keep the existing Markdown as the content source and extend only each deck's embedded `custom_css`, plus non-rendered class markers or wrapper markup when needed to express an existing layout without changing its text. Use shared slide-family rules for vertical centering, grids, section breaks, title cards, code panels, plots, metrics, callouts, source notes, and footers. Render both decks to temporary directories, capture every slide, inspect all previews, then synchronize generated artifacts to both requested build directories.

**Tech Stack:** Colloquium Markdown, CSS, installed `colloquium` CLI, Chromium capture, Pillow montage generation, pytest.

---

### Task 1: Apply shared presentation-system CSS to both sources

**Files:**
- Modify: `/Users/noeflandre/osm-polygon-description-tag/.worktrees/dataset-slide-06/slides/dataset.md`
- Modify: `/Users/noeflandre/osm-polygon-description-tag/.worktrees/dataset-slide-06/slides/codebase.md`

- [ ] **Step 1: Add natural-flow vertical centering and safe areas**

In both `custom_css` blocks, add rules that center content slides and section breaks while leaving title-slide behavior intact:

```css
  .pad-roomy.active { padding: 72px; }
  .slide--content { justify-content: center; }
  .slide--content > .slide-content,
  .slide--section-break > .slide-content {
    flex: 0 0 auto;
  }
  .slide--content > .slide-content { width: min(100%, 70rem); margin-left: auto; margin-right: auto; }
  .slide--section-break { justify-content: center; }
  .slide--section-break > .slide-content { width: min(100%, 70rem); margin-left: auto; margin-right: auto; }
```

- [ ] **Step 2: Normalize notes, grids, code panels, and callouts**

Add these shared rules, preserving each deck’s existing colors:

```css
  .source-note {
    bottom: 3.1rem;
    letter-spacing: 0.01em;
  }
  .slide--content.cols-2 .colloquium-grid,
  .slide--content.cols-3 .colloquium-grid { align-items: start; }
  .slide--content.cols-2 .colloquium-grid > .col + .col,
  .slide--content.cols-3 .colloquium-grid > .col + .col {
    border-left: 1px solid var(--colloquium-border);
    padding-left: 2.25rem;
  }
  .slide pre { margin: 1rem auto; }
  .slide--content:not(.cols-2):not(.cols-3) .slide-content > pre { max-width: 70rem; }
  .slide--title .slide-content pre {
    width: max-content;
    max-width: 100%;
    margin-left: auto;
    margin-right: auto;
  }
  .dark-note {
    margin-top: 0.75rem;
    margin-bottom: 0.25rem;
    background: #eef2ee;
  }
  .slide--section-break .source-note { color: rgba(246, 245, 240, 0.68); }
```

Use a root-relative unit for code sizing only if needed after render inspection; do not reduce code readability to solve whitespace.

### Task 2: Refine dataset slide families without changing text

**Files:**
- Modify: `/Users/noeflandre/osm-polygon-description-tag/.worktrees/dataset-slide-06/slides/dataset.md`

- [ ] **Step 1: Make balanced slides use the same natural-flow anchor**

Replace the existing `.balanced { padding-top: 110px; }` declaration with:

```css
  .balanced {
    justify-content: center;
  }
  .balanced > .slide-content { flex: 0 0 auto; }
```

Keep the existing `metric-slide` rules, but let them inherit the same centering behavior.

- [ ] **Step 2: Improve metric and plot rhythm**

Use scoped rules:

```css
  .metric-slide .metric-block { min-height: 7rem; }
  .metric-slide .metric-summary { border-top: 2px solid var(--colloquium-accent); }
  .cols-3 .big-number + .metric-label,
  .metric-slide .big-number + .metric-label { margin-top: 0.35rem; }
  .plot-slide .slide-content > p:first-child { margin-bottom: 0.65rem; }
  .plot-slide .slide-content > p:nth-child(2) { margin-top: 0; text-align: center; }
```

- [ ] **Step 3: Give the guardrail and timestamp slides a deliberate reading width**

Add non-rendered class comments to those two slides only, then use:

```css
  .guardrail-slide > .slide-content {
    width: min(100%, 62rem);
    margin-left: auto;
    margin-right: auto;
  }
  .guardrail-slide .slide-content > p { margin-bottom: 1.05rem; }
  .timestamp-slide .colloquium-grid > .col { min-height: 7rem; }
  .timestamp-slide .colloquium-grid > .col:first-child { display: flex; flex-direction: column; justify-content: center; }
  .timestamp-slide .colloquium-grid > .col + .col { display: flex; align-items: center; }
```

Do not alter the paragraph or metric text when adding the class comments.

### Task 3: Refine codebase slide families without changing text

**Files:**
- Modify: `/Users/noeflandre/osm-polygon-description-tag/.worktrees/dataset-slide-06/slides/codebase.md`

- [ ] **Step 1: Add the shared presentation rules from Task 1**

Apply the same rules and values, adjusted only if a full-size capture shows code or tables clipping.

- [ ] **Step 2: Constrain and center single-column diagrams**

Use:

```css
  .slide--content:not(.cols-2):not(.cols-3) .slide-content > pre {
    width: min(100%, 70rem);
  }
```

Keep code text unchanged. The rule only changes the panel width and horizontal placement.

- [ ] **Step 3: Keep the section-break diagram legible**

Use:

```css
  .slide--section-break .slide-content > pre {
    width: min(100%, 58rem);
    margin-left: auto;
    margin-right: auto;
  }
```

### Task 4: Render, inspect, and iterate

**Files:**
- Generate: `/tmp` temporary dataset and codebase HTML/PNG outputs
- Synchronize: `/Users/noeflandre/osm-polygon-description-tag/slides/build/dataset/`
- Synchronize: `/Users/noeflandre/osm-polygon-description-tag/slides/build/codebase/`

- [ ] **Step 1: Build and capture every slide in both decks**

Run `colloquium build` and `colloquium capture` for each source in one shell per deck, copying the deck assets into each temporary output. Capture all slides, not only the changed ones.

- [ ] **Step 2: Inspect all full-size previews**

Read dataset slides 1-11 and codebase slides 1-12. Check that every title, body block, grid, diagram, callout, source note, and footer is visible, centered or intentionally aligned, and free of clipping. If a rule creates an imbalance or overflow, adjust only the CSS or a non-rendered class hook and recapture.

- [ ] **Step 3: Create and inspect both montages**

Create 3×4 montages with 480×270 thumbnails and 12px black gutters. Inspect the complete sequence for rhythm consistency across adjacent slides.

- [ ] **Step 4: Verify content preservation**

Extract text-bearing HTML and compare the headings, body strings, numbers, code blocks, links, and source notes against the authored Markdown. Confirm only CSS/classes/layout wrappers changed in the source diff.

- [ ] **Step 5: Run validation**

Run:

```bash
cd /Users/noeflandre/osm-polygon-description-tag/.worktrees/dataset-slide-06
uv run pytest -q
git diff --check
```

Expected: 612 tests pass, no whitespace errors, and no generated artifacts are accidentally staged as authored content.
