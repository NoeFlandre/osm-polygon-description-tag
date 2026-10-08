# CRAP on final head d7f63f1

Coverage source: the `.coverage` data written by the unit + contracts run on d7f63f1. Checked: the data is newer than the commit, and no source or test file is newer than the data. The suite was not re-run for this step. `coverage json` was exported from that data.

Commands (same steps as the justfile `risk-prepared` recipe):
- `radon cc src/osm_polygon_description_tag scripts -s -j > reports/radon-cc.json`
- `python scripts/quality_metrics.py crap --coverage-json reports/coverage.json --radon-json reports/radon-cc.json --output reports/crap.json --markdown-output reports/crap.md`
- `python scripts/quality_metrics.py check --report reports/crap.json --max-crap-score 6`

Results:
- check: exit 0, "all scores < 6".
- Functions scored: 1716. Unmatched: 0.
- Repo maximum: 5.47 (`scripts/show_mutant.py`, `_functions_in.walk`).
- Changed functions in `dataset/docs.py`:
  - `_write_dataset_docs`: CRAP 3.0 (complexity 3, coverage 100%).
  - `_atomic_write_if_changed`: CRAP 3.0 (complexity 3, coverage 100%).
  - `_write_dataset_hero`: CRAP 1.0 (complexity 1, coverage 100%).
- Highest in `dataset/docs.py`: 5.0 (`_fmt_area` and three others). Not changed on this branch.
- Generated outputs (`reports/`) are not uploaded. The values above are the record.
