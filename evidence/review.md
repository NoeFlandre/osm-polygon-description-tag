# Review record (issue #138, description-tag)

Five independent reviews were run. Each was a fresh subagent that had no part in writing the code it reviewed. Each reported its model as claude-haiku-5-5, from its own environment; this is self-reported and not verified.

## Review 1: base ea22740 to cc395b9 (full change)
Verdict: APPROVE WITH FINDINGS. No blockers. All findings fixed in later rounds, except the CRLF note, which is now a known pinned defect.
- Narrow CRLF note (README path not covered). Fixed in round 3.
- README UTF-8 check covered only ASCII; a test encoded in the test itself. Fixed in round 2 (real-path test added).
- JSON expected bytes computed by the same formula as the code under test. Fixed: literal bytes.
- Misleading test names and a duplicate test. Fixed in round 2.
- Note only: the encode order changed in `_write_dataset_docs` (see Review 4). Reviewer judged it unreachable.

## Review 2: round 2 delta, 40952cd
Verdict: APPROVE WITH FINDINGS. Findings fixed in round 3:
- README check was tail-only; the full README was not pinned.
- Two substring-only checks were redundant with exact checks.
- Reporting test that did not pin mtime.
- Comment on the CRLF test claimed docs.py had the same cause, without a test.

## Review 3: round 3 delta, 13ace50 to round-3 commit
Verdict: APPROVE WITH FINDINGS. Findings fixed in round 4:
- Writer-level UTF-8 check was missing for `_atomic_write_if_changed`. Added.
- Reporting no-op test did not pin mtime. Fixed by pinning mtime to a past value.
- CRLF comment did not say that docs.py reads the same way. Fixed.

## Review 4: complete final diff ea22740 to d7f63f1
Verdict: APPROVE WITH FINDINGS. No high or medium findings. Scope: 5 commits, 4 files, plus the round 4 delta. Two LOW findings, NOT FIXED:
- LOW 1: commit `cc395b9` says mutation scope is unchanged. Not exact. The two write calls in `_write_dataset_docs` are now mutable. The docs.py mutation run shows they are covered by the tests. Put this in the PR body. Do not rewrite history.
- LOW 2: one runtime test uses `monkeypatch.undo()`, which also reverts the autouse hf, Popen and which guard from `tests/conftest.py`. No effect today. Preferred fix: `with monkeypatch.context():`.
Info items:
- Encode order in `_write_dataset_docs`: both strings are encoded before either write. Before the change, stats.json was written before README was encoded. The reviewer found no reachable input that changes behaviour. Callers: `generate_dataset_docs`, reached from the CLI, release, finalization and orchestration. The README text is strict UTF-8 template text, and the stats block was encoded earlier.
- The pinned CRLF-to-LF test passes today and locks the defect. This is intended, and the comment says so.
- Expected stats bytes in one older test are rebuilt with the code's own `json.dumps` formula. Pre-existing. The literal-bytes test covers it.
- Mode (permissions) is checked only for a new file, not for a replace. Not a regression.
- Some coverage is duplicated between the reporting helper test and the runtime contracts file.

Pragmas: the review found only moved pragma lines. No skip, xfail, noqa, type-ignore or new pragma was added.
Scope: card.py, publication/state.py, tests/unit/dataset/test_geography_card.py and the PR #168 files are unchanged on the branch.
Commit trailers: no model names in any commit message. Only the Claude-Session trailer.

## Review 5: not a review. CRLF investigation
See `crlf/issue-draft.md`. It gives evidence for and against a settled contract. It makes no verdict.

## Out-of-scope note
`tests/unit/dataset/test_geography_card.py` lines 853-856 (a comment, in PR #145's file) say the card writer keeps CRLF byte-for-byte. The pinned runtime test says the file path does not. That comment is not on this branch.
