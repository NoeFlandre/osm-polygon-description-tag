# Dataset Card Hero Image Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Publish one canonical branding image as the first visible element of the GitHub README and generated Hugging Face dataset card.

**Architecture:** Store the canonical PNG in repository `assets/`, package it as application data, and copy its exact bytes into the generated dataset root during documentation generation. Extend strict publication validation and both upload-plan types so the required hero travels with all dataset-card metadata.

**Tech Stack:** Python 3.12, pathlib/importlib resources, pytest, Ruff, ty, MkDocs Material, Hugging Face CLI.

---

## File map

- `assets/dataset-card-hero.png`: canonical source image and GitHub-rendered asset.
- `src/osm_polygon_description_tag/_data/dataset-card-hero.png`: packaged resource copied from the canonical asset by the build configuration.
- `README.md`: GitHub README, displaying the canonical asset first.
- `src/osm_polygon_description_tag/_data/dataset-card-template.md`: runtime Hugging Face card template.
- `docs/dataset-card-template.md`: maintained source mirror of the packaged card template.
- `pyproject.toml`: package-data inclusion for PNG resources.
- `src/osm_polygon_description_tag/runtime/resources.py`: typed accessor for the packaged hero.
- `src/osm_polygon_description_tag/dataset/reporting.py`: deterministic installation of the hero in the data root.
- `src/osm_polygon_description_tag/publication/planning.py`: required-asset validation and upload-plan inclusion.
- `tests/contracts/test_dataset_card.py`, `tests/unit/dataset/test_reporting.py`: generation behavior.
- `tests/unit/dataset/test_geography_publication.py`, `tests/unit/publication/test_per_pbf_plan.py`, `tests/unit/publication/test_publication.py`: publication validation and plan contents.
- Existing contract and integration fixtures that construct complete `assets/` directories: add the newly required hero fixture.

### Task 1: Establish the canonical and packaged image

**Files:**
- Move: `PNG image.png` to `assets/dataset-card-hero.png`
- Create: `src/osm_polygon_description_tag/_data/dataset-card-hero.png`
- Modify: `pyproject.toml:49-54`

- [ ] **Step 1: Rename the user-provided file without changing bytes**

Run:
```bash
mv "PNG image.png" assets/dataset-card-hero.png
```
Expected: `assets/dataset-card-hero.png` exists and the root filename no longer exists.

- [ ] **Step 2: Add PNG package-data inclusion and copy the canonical bytes**

Add to Hatch's `include` list:
```toml
"src/osm_polygon_description_tag/_data/*.png",
```

Then run:
```bash
cp assets/dataset-card-hero.png src/osm_polygon_description_tag/_data/dataset-card-hero.png
cmp assets/dataset-card-hero.png src/osm_polygon_description_tag/_data/dataset-card-hero.png
```
Expected: `cmp` exits 0.

- [ ] **Step 3: Verify the package contains the resource**

Run:
```bash
uv build
python -c "import zipfile,glob; p=glob.glob('dist/*.whl')[-1]; z=zipfile.ZipFile(p); assert any(n.endswith('/_data/dataset-card-hero.png') for n in z.namelist())"
```
Expected: build and assertion succeed.

- [ ] **Step 4: Commit the asset foundation**

```bash
git add assets/dataset-card-hero.png src/osm_polygon_description_tag/_data/dataset-card-hero.png pyproject.toml
git commit -m "feat: package dataset card hero image"
```

### Task 2: Render the hero at the top of both cards

**Files:**
- Modify: `README.md:1`
- Modify: `src/osm_polygon_description_tag/_data/dataset-card-template.md:13`
- Modify: `docs/dataset-card-template.md:13`
- Test: `tests/contracts/test_dataset_card.py`

- [ ] **Step 1: Write a failing dataset-card ordering assertion**

In the generated-card contract, assert:
```python
hero = "![OSM Polygon Description Tag dataset hero](assets/dataset-card-hero.png)"
assert hero in readme
assert readme.index(hero) < readme.index("# OSM Polygon Description Tag")
```

- [ ] **Step 2: Run the focused test and verify failure**

Run:
```bash
uv run pytest tests/contracts/test_dataset_card.py -q
```
Expected: FAIL because the hero reference is absent.

- [ ] **Step 3: Add the image references**

Make the GitHub README begin with:
```markdown
![OSM Polygon Description Tag dataset hero](assets/dataset-card-hero.png)

# OSM Polygon Description Tag
```

Immediately after the closing YAML delimiter in both dataset-card templates, add:
```markdown
![OSM Polygon Description Tag dataset hero](assets/dataset-card-hero.png)

```

- [ ] **Step 4: Verify template mirror and focused test**

Run:
```bash
cmp docs/dataset-card-template.md src/osm_polygon_description_tag/_data/dataset-card-template.md
uv run pytest tests/contracts/test_dataset_card.py -q
```
Expected: both commands pass.

- [ ] **Step 5: Commit rendering changes**

```bash
git add README.md docs/dataset-card-template.md src/osm_polygon_description_tag/_data/dataset-card-template.md tests/contracts/test_dataset_card.py
git commit -m "feat: display dataset hero on repository cards"
```

### Task 3: Install the packaged hero during card generation

**Files:**
- Modify: `src/osm_polygon_description_tag/runtime/resources.py`
- Modify: `src/osm_polygon_description_tag/dataset/reporting.py`
- Test: `tests/unit/dataset/test_reporting.py`

- [ ] **Step 1: Write a failing generation test**

Add a test that runs `generate_dataset_docs(...)` and verifies:
```python
hero = data_root / "assets" / "dataset-card-hero.png"
assert hero.read_bytes() == dataset_card_hero().read_bytes()
```
Also run generation twice and assert the hero mtime is unchanged when bytes match.

- [ ] **Step 2: Run the test and verify failure**

Run the exact new test node with:
```bash
uv run pytest tests/unit/dataset/test_reporting.py -k hero -q
```
Expected: FAIL because `dataset_card_hero` and copying behavior do not exist.

- [ ] **Step 3: Add the resource accessor**

In `runtime/resources.py` add:
```python
def dataset_card_hero() -> Path:
    return resource_path("dataset-card-hero.png")
```

- [ ] **Step 4: Add deterministic binary installation**

In `dataset/reporting.py`, add an atomic binary helper mirroring `_write_if_changed`:
```python
def _write_bytes_if_changed(path: Path, data: bytes) -> bool:
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.is_file() and path.read_bytes() == data:
        return False
    temp = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
    try:
        temp.write_bytes(data)
        with open(temp, "rb") as handle:
            os.fsync(handle.fileno())
        os.replace(temp, path)
        return True
    finally:
        if temp.exists():
            temp.unlink()
```
Import `dataset_card_hero`, define `assets/dataset-card-hero.png`, and call the helper in `generate_dataset_docs` before final output writes.

- [ ] **Step 5: Run focused reporting tests**

Run:
```bash
uv run pytest tests/unit/dataset/test_reporting.py tests/contracts/test_dataset_card.py -q
```
Expected: PASS, including unchanged mtime on identical regeneration.

- [ ] **Step 6: Commit generation support**

```bash
git add src/osm_polygon_description_tag/runtime/resources.py src/osm_polygon_description_tag/dataset/reporting.py tests/unit/dataset/test_reporting.py
git commit -m "feat: install hero during dataset card generation"
```

### Task 4: Require and upload the hero asset

**Files:**
- Modify: `src/osm_polygon_description_tag/publication/planning.py:37-150,273-345`
- Test: `tests/unit/dataset/test_geography_publication.py`
- Test: `tests/unit/publication/test_per_pbf_plan.py`
- Test: `tests/unit/publication/test_publication.py`
- Modify: all existing test fixtures that build a complete `assets/` directory.

- [ ] **Step 1: Add failing allowlist and plan assertions**

Define expected asset path `assets/dataset-card-hero.png`. Assert that:
```python
assert "assets/dataset-card-hero.png" in {item.relative_path for item in plan.files}
```
for per-PBF and metadata-only plans, and assert a missing hero raises `PublicationError` mentioning `dataset-card-hero.png`.

- [ ] **Step 2: Run focused publication tests and verify failure**

Run:
```bash
uv run pytest tests/unit/dataset/test_geography_publication.py tests/unit/publication/test_per_pbf_plan.py tests/unit/publication/test_publication.py -q
```
Expected: FAIL because the third required asset is unknown or absent from plans.

- [ ] **Step 3: Extend publication constants and validation**

Add:
```python
DATASET_CARD_HERO_FILENAME = "dataset-card-hero.png"
DATASET_CARD_HERO_ASSET_RELATIVE = f"assets/{DATASET_CARD_HERO_FILENAME}"
_ALLOWED_ASSET_FILES = frozenset(
    {H3_MAP_FILENAME, AREA_HISTOGRAM_FILENAME, DATASET_CARD_HERO_FILENAME}
)
```
Change `_validate_assets_for_publication` to return three canonical items and require the hero. Include all three in `_build_per_pbf_upload_plan` and `_build_metadata_only_upload_plan`; update exact-file-count docstrings from 6/4 to 7/5.

- [ ] **Step 4: Update complete-assets test fixtures**

Where a fixture currently writes both existing PNGs, also write:
```python
(data_root / "assets" / "dataset-card-hero.png").write_bytes(
    b"\x89PNG\r\n\x1a\n" + b"hero" * 1024
)
```
Do not weaken unknown-file rejection tests.

- [ ] **Step 5: Run publication, contract, and integration tests**

Run:
```bash
uv run pytest tests/unit/publication tests/unit/dataset/test_geography_publication.py tests/contracts tests/integration -q
```
Expected: PASS with 7-file per-PBF and 5-file metadata plans.

- [ ] **Step 6: Commit publication support**

```bash
git add src/osm_polygon_description_tag/publication/planning.py tests
git commit -m "feat: publish dataset card hero asset"
```

### Task 5: Validate locally and synchronize remotes

**Files:**
- Generated outside repository: `/Volumes/Seagate M3/projects/osm-polygon-description-tag/README.md`
- Generated outside repository: `/Volumes/Seagate M3/projects/osm-polygon-description-tag/assets/dataset-card-hero.png`

- [ ] **Step 1: Run all repository checks**

Run:
```bash
just check
uv run mkdocs build --strict
```
Expected: formatting, lint, types, full tests, coverage gate, build, and strict documentation build all pass.

- [ ] **Step 2: Regenerate the real dataset card**

Run:
```bash
uv run osm-polygon-description-tag generate-card
cmp assets/dataset-card-hero.png "/Volumes/Seagate M3/projects/osm-polygon-description-tag/assets/dataset-card-hero.png"
```
Expected: generation succeeds and bytes match exactly.

- [ ] **Step 3: Inspect and execute the Hugging Face publication plan**

Run:
```bash
uv run osm-polygon-description-tag publish-plan
```
Confirm the plan includes `README.md` and `assets/dataset-card-hero.png`, then execute the repository's printed publish command with its exact identity:
```bash
uv run osm-polygon-description-tag publish --plan <printed-identity>
```
Expected: upload and existing Hub SHA-256 verification succeed.

- [ ] **Step 4: Review the final repository commit contents**

Run:
```bash
git status
git diff --cached
git diff HEAD~4..HEAD
```
Inspect every changed file and image path for secrets or unintended artifacts. Stage and commit any check-generated source fixes only after review.

- [ ] **Step 5: Push GitHub after final verification**

Run:
```bash
git push origin main
```
Expected: `origin/main` advances to the local verified commits.

- [ ] **Step 6: Confirm clean synchronization state**

Run:
```bash
git status -sb
git fetch origin
git status -sb
```
Expected: local `main` matches `origin/main`, no untracked root image remains, and Hugging Face publication previously completed with remote verification.
