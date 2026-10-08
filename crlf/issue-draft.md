# Draft. Not posted.

## Title

CRLF card text becomes LF on read in `card.py` and `docs.py`

## Summary

Two read paths load card text with `Path.read_text(encoding="utf-8")`. That call uses universal newline mode, so CRLF becomes LF before the text is rewritten and written back. A reproducer with a CRLF template shows no CR bytes in the output on either path. The write side (`atomic_write_text`, `newline=""`) does not translate. This appears to be a newline conversion on read. It is not yet established as a bug. The repo states a byte-preservation intent in several places, but no test or doc states a newline policy for templates or READMEs in both directions, and the owner has not yet said which contract applies.

## Reproducer

Run from the repo root with the repo's virtualenv. The script reads the source path from `REPO_SRC` (it inserts it into `sys.path` itself, because `-I` ignores `PYTHONPATH`):

    REPO_SRC=<repo>/src <repo>/.venv/bin/python -I crlf/repro_crlf.py

Script: `crlf/repro_crlf.py`. It writes only to system temp dirs. Saved output: `crlf/repro_output.txt`. Exit code 0. The output was re-checked byte-for-byte against a fresh clone of the code branch.

Exact output:

```
A before bytes: b'---\r\ntitle: caf\xc3\xa9\r\n---\r\n\r\nIntro text.\r\n\r\n<!-- GENERATED:STATS:START -->\r\nstats\r\n<!-- GENERATED:STATS:END -->\r\n'
```

(The full exact output is in `crlf/repro_output.txt`; this draft quotes it in full there.)

Path A calls `write_map_block_marker_to_template`. Path B calls `_card_source` with `preserve_existing=False` (template branch). Path C calls `_card_source` with `preserve_existing=True` (a `README.md` in the data root). The full `_write_dataset_docs` path was not run, because it needs a full stats dict. Paths B and C cover only the read step.

## Observed behaviour

- Path A: `dataset/geography/card.py:157` `template_path.read_text(encoding="utf-8")`. The text has no `\r` after this line, so `_newline_for(text)` at `card.py:180` always returns `"\n"`. The file is written back as LF (`card.py:193`).
- Path B and C: `dataset/docs.py:475` and `dataset/docs.py:478` `read_text(encoding="utf-8")`. The returned text has no CR. `_update_stats_block` picks its newline with `_newline_for(readme)` at `docs.py:568`, which is always LF. The bytes are written with `readme.encode("utf-8")` at `docs.py:617`.
- Write side: `runtime/atomic.py:38` writes with `newline=""`, so it does not translate.

## Stated contract (quotes)

Write-side and byte-preservation intent:

- `runtime/atomic.py:37`: "Durably replace ``path`` with ``text`` encoded as UTF-8, without newline translation."
- `dataset/geography/card.py:6-7` (module docstring): "the surrounding handwritten prose is preserved byte-for-byte."
- `dataset/geography/card.py:109-110` (`install_map_block`): "Outside the marker block, the template is preserved byte-for-byte."
- `dataset/geography/card.py:176-179` (comment): "so the surrounding prose is preserved byte-for-byte."
- `dataset/docs.py:469-470` (`_card_source` docstring): "A release explicitly opts into the existing card so remote language annotations and other published prose remain byte-for-byte intact."
- `tests/unit/dataset/test_geography_card.py:853-856` (comment): "A dataset card authored on Windows arrives with CRLF line endings. The card writer preserves the template byte-for-byte outside the marker block, so the CRLF branch of every newline helper is part of that guarantee."
- `tests/unit/runtime/test_atomic_wrapper_contracts.py:157-160` (test name and comment): "KNOWN DEFECT (pinned, not desired): read_text in dataset/geography/card.py converts CRLF to LF, but the template should keep its bytes. A fix must change this test." The test asserts the LF output (`lines 170-171`).

Read-side intent:

- `publication/verification.py:239`: `text.replace("\r\n", "\n").replace("\r", "\n")`. This normalization is intentional for Hub reads and is pinned by `tests/unit/publication/test_verification_mutation_survivors.py:108` (`test_read_file_decodes_utf8_and_normalizes_universal_newlines`).
- No read-side rule exists for `card.py` or `docs.py`.

Other reads in the repo: `publication/language_hub.py:260` uses `path.read_bytes().decode("utf-8")`, which keeps CRLF.

In-memory CRLF tests (they do not run a CRLF file through the read path): `tests/unit/dataset/test_geography_card.py:882-883`, `tests/unit/dataset/test_docs_helpers.py:397-401` and `:409-415`, `tests/unit/publication/test_release_release_preserves_existing.py:211-212`, `tests/unit/publication/test_language_card.py:264`.

Docs: no README, CONTRIBUTING, or docs page states a newline policy for templates or generated READMEs. `.gitattributes` has one rule (`src/osm_polygon_description_tag/_data/sat-capabilities.json text eol=lf`) and none for the card template.

## Impact assessment

Repo files today:

- Tracked files: 440. Index eol: `i/lf` 429, `i/-text` 5 (binary PNGs), `i/none` 6 (empty), `i/crlf` 0.
- Per folder, `i/crlf` count: `src/` 0 of 121, `docs/` 0 of 13, `config/` 0 of 1, `tests/` 0 of 258, `scripts/` 0 of 11.
- Card template: `src/osm_polygon_description_tag/_data/dataset-card-template.md` and `docs/dataset-card-template.md` are both `i/lf w/lf` and byte-identical. There is no tracked README template for the dataset card, because `data_root/README.md` is generated.

Reachability:

- `write_map_block_marker_to_template`: no production caller in `src/`, `scripts/`, or `benchmarks/`. Only tests call it (`tests/unit/runtime/test_atomic_wrapper_contracts.py`, `tests/unit/dataset/test_geography_card.py`). Not reachable from the public CLI.
- `_card_source`: reached through `_write_dataset_docs` (`docs.py:608`) from `generate_dataset_docs`. Callers: `cli.py:360` (`handle_card`), `cli.py:410` via `release_metadata` (`release.py:257`), `workflow/finalization.py:133`, and `workflow/orchestrator.py:790`. Every caller passes `dataset_card_template()`, the packaged file. The CLI has no template argument. A user-supplied template cannot reach this path through the CLI.
- Release flow: `release.py:249` runs `_sync_remote_card`, which writes the Hub README. The Hub text comes through `verification.py:239`, which already converts CRLF to LF. So in the release flow, `docs.py` reads an LF file. The CRLF-to-LF change in that flow happens earlier, at `verification.py:239`.

Verdict (evidence only, not a decision):

- No tracked repo file is affected today.
- Real inputs that would be affected: (1) a CRLF card template passed to `write_map_block_marker_to_template`, which has no production caller today; (2) a CRLF `data_root/README.md` read with `preserve_existing=True` when no Hub sync overwrites it. Case (2) was not run end to end; (3) a Hub README that is CRLF. That case is handled by the intentional normalization at `verification.py:239`, a separate question.

## Options

(a) Keep LF normalization as the contract. Document it in the `card.py` and `docs.py` docstrings. Change the claim at `docs.py:469-470`, because it is false for CRLF input (Path C). Replace the "KNOWN DEFECT" test at `test_atomic_wrapper_contracts.py:157` with a pin of the LF result, and update the comment at `test_geography_card.py:853-856`. Keep the write side as is.

(b) Preserve CRLF bytes. Read the bytes and decode on both read paths (`read_bytes().decode("utf-8")`, as `language_hub.py:260` does, or `read_text(newline="")`). Keep the write side as is. Update the test at `test_atomic_wrapper_contracts.py:157` to expect CRLF, and add file-level CRLF tests for both paths. The stats and map blocks on CRLF input need their own test run. This was not run here.

(c) Decide later with a sample of real templates and Hub READMEs. The repo has no CRLF file today (index count 0), so this would mean collecting outside samples first.

## Question for the owner

Which contract is intended: (a) LF normalization on read, (b) byte preservation on read, or (c) undecided? Is the "KNOWN DEFECT (pinned, not desired)" label at `tests/unit/runtime/test_atomic_wrapper_contracts.py:158` still current?

Draft. Not posted.
