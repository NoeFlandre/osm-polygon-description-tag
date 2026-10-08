# Mutation check: dataset/docs.py only (description-tag, final head d7f63f1)

## What this is, and what it is not
- It IS: a mutation run on one file, `src/osm_polygon_description_tag/dataset/docs.py`, using the repo's gate script with `--only-mutate` on that file, and test selection `tests/unit` and `tests/contracts` only.
- It is NOT: a repo-wide mutation result. The repo's `just mutation` recipe was not run. This is not the full gate.
- Acceptance and integration tests (29) were excluded from the contexts, the clean run and every mutant run. The justfile recipe is wider. More tests can only add kills, so the kills should hold, but this is not verified.
- No config file was changed: no `pyproject.toml`, `justfile`, or gate-script edit. Scope is set on the command line only.

## Command
The exact command is in `mutation/run_docs_mutation.sh`. Its key line:

    PYTEST_ADDOPTS='-m "not acceptance and not integration"' timeout 2400 uv run python -m scripts.run_mutation_gate --max-children 8 --coverage-file data-root/.tmp/.coverage-ctx --only-mutate src/osm_polygon_description_tag/dataset/docs.py --test-selection tests/unit --test-selection tests/contracts

Then the gate check:

    check_mutation_score.py --mutants-root mutants --scope-file scope_docs.txt --minimum-score 100

## Results
- Mutants generated for docs.py: 924 (dry generation, no tests run). Name format: `osm_polygon_description_tag.dataset.docs.x_<function>__mutmut_<n>`.
- Context step: `4150 passed, 4 skipped`, exit 0, 175 s wall (177 s with setup). The skip count differs from the full unit + contracts run (5). Not investigated.
- Mutation job: exit 0, 917 s wall, not capped. Escalation passes ran at about 10.8, 3.9 and 0.07 mutants per second.
- Final gate: `mutation score: 100.00% (924/924); minimum 100.00%`, exit 0.
- Final counts: killed 924, survived 0, no-tests 0, timeouts 0, suspicious 0, skipped 0, caught by type check 0.
- Machine: 4 CPUs.

## Intermediate survivors (correction to an earlier figure)
A first pass left 425 unique mutants alive. All 425 were killed in later passes, and all are in the final killed list (`mutation/killed.txt`). An earlier note said 20. That was wrong. The exact list is in `mutation/intermediate-survivors-later-killed.txt`.

## Files
- `mutation/killed.txt`: the 924 killed mutant names.
- `mutation/intermediate-survivors-later-killed.txt`: 425 names (445 lines, 20 duplicates from repeated passes).
- `mutation/contexts.log`, `mutation/check_docs.log`, `mutation/timing.txt`, `mutation/scope_docs.txt`: run records.
- `mutation/count_docs_mutants.py`: the dry count script.
- Raw progress log (314 KB, mostly progress lines) is not uploaded. Its verdict lines are summarised above.
- Mutant source copies and mutmut's cache are not uploaded. They are generated.

## Pragmas
Lines with `# pragma: no mutate` are skipped by mutmut at generation. The tool does not report them separately, so they are not counted here.
