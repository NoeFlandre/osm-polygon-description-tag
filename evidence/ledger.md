# Ledger (second master) — updated 2026-10-08

## Ownership checks (done)
- Both repos fetched with prune. Designated branch `claude/atomic-write-mutation-coverage-go783s` existed only locally, at origin/main, with no unique commits. Its remote branch was pruned in both repos.
- No open, draft, closed or merged PR references issue #138 (desc) or #108 (eunis).
- File overlaps with open PRs:
  - desc #145 touches tests/unit/dataset/test_geography_card.py. This blocks the card.py wrapper removal.
  - desc #168 (off-limits) touches dataset/geography/atomic.py, migration.py, storage.py, text_migration.py, workflow/grid_operator/state.py.
  - eunis #100 (dependabot) touches pyproject.toml lines 12 and 30. No hunk overlap with the [tool.mutmut] block.
  - eunis #136 (first master) touches .github/workflows/{mutation,performance,qa,release}.yml. Off-limits. mutation.yml does not hardcode modules, so #108 needs no workflow edit.
- Uncertainty: public GitHub state cannot rule out unpublished work by another agent.

## #138 (osm-polygon-description-tag) — PARTIAL, issue stays open
| Item | Status |
|---|---|
| Branch | claude/atomic-write-mutation-coverage-go783s (local only, not pushed) |
| Base | ea22740443e526836590c267afa8ec87db2c222d |
| Candidate | 13ace50 |
| Commits | 5c7f94f (characterization tests) · cc395b9 (remove _write_if_changed) · 40952cd (review fixes) · 13ace50 (review round 3) |
| Changed files | dataset/docs.py; tests/unit/dataset/test_docs_helpers.py; tests/unit/dataset/test_reporting_helpers.py; NEW tests/unit/runtime/test_atomic_wrapper_contracts.py |
| Removed | docs.py `_write_if_changed` (no callers left) |
| Blocked | card.py `_atomic_write_template`: needs test_geography_card.py edits, which overlap open PR #145. Owner decision needed. |
| Kept | publication/state.py `_atomic_write_json`: pretty JSON != canonical JSON, so it is not a duplicate |
| Kept | docs.py `_atomic_write_if_changed`: adds the no-op check |
| Finding | CRLF templates become LF on card and README paths (read_text). Pinned as KNOWN DEFECT. Needs a follow-up issue and owner decision. |
| Checks on 13ace50 | focused 4 files: 89 passed · ruff format/check: pass · ty: pass · full default suite (ignores acceptance, integration): 4149 passed, 5 skipped · coverage 99.13% branch (gate 90%) |
| CRAP | `check --max-crap-score 6`: pass on 40952cd (1716 functions). Not re-run on 13ace50 (test-only change). |
| Mutation | NOT RUN. The recipe is repo-wide. Only a docs.py-only run via check_mutation gate is possible. |
| Review | Reviewer 1 (cc395b9): approve with findings, all fixed. Reviewer 2 (40952cd): fixes done in round 3. Reviewer 2 did not re-review round 3. |
| Model | Subagents report claude-haiku-5-5 from their own context. Not verified from outside. |

## #108 (osm-polygon-eunis) — IN PROGRESS (bounded trial)
| Item | Status |
|---|---|
| Branch | claude/atomic-write-mutation-coverage-go783s (local) |
| Base | 35ce838a444e787bc91432586916b9be6a205ef9 |
| Targets | src/osm_polygon_eunis/manifest_state.py (385 lines), publish.py (482 lines) |
| Tests | tests/unit/test_manifest_state.py (11), tests/unit/test_publish.py (17) |
| Status | Trial running (agent <id>). No commits. Cost report pending. |
