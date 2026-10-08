#!/usr/bin/env bash
# Scoped mutation check for src/osm_polygon_description_tag/dataset/docs.py.
# Uses only CLI options and env vars; no repo config is edited.
# Acceptance and integration tests are excluded on purpose (verification rule).
set -u
R=<desc-repo>
SCR=<scratchpad>/scratchpad
UV=<root>/.local/bin/uv
SCOPE="$SCR/mut/scope_docs.txt"
cd "$R" || exit 99
mkdir -p data-root/.tmp reports
printf 'src/osm_polygon_description_tag/dataset/docs.py\n' > "$SCOPE"

echo "nproc=$(nproc)" > "$SCR/mut/timing.txt"

# 1. Coverage contexts (same flags as the justfile mutation-contexts recipe),
#    limited to unit + contracts; the -m filter drops integration/acceptance markers.
t0=$(date +%s)
COVERAGE_FILE="$R/data-root/.tmp/.coverage-ctx" timeout 1200 "$UV" run pytest -q -p no:cacheprovider -n auto \
  --cov=osm_polygon_description_tag --cov-branch --cov-context=test --cov-report= \
  tests/unit tests/contracts -m "not acceptance and not integration" \
  > "$SCR/mut/contexts.log" 2>&1
echo "contexts_exit=$?" >> "$SCR/mut/contexts.log"
t1=$(date +%s)
echo "contexts_wall_s=$((t1-t0))" >> "$SCR/mut/timing.txt"

# 2. The single scoped mutation job: docs.py only, justfile max-children 8.
#    PYTEST_ADDOPTS keeps the marker filter on every pytest call mutmut makes.
#    --test-selection stops the clean run from collecting tests/acceptance and tests/integration.
t2=$(date +%s)
PYTEST_ADDOPTS='-m "not acceptance and not integration"' timeout 2400 "$UV" run python -m scripts.run_mutation_gate \
  --max-children 8 \
  --coverage-file data-root/.tmp/.coverage-ctx \
  --only-mutate src/osm_polygon_description_tag/dataset/docs.py \
  --test-selection tests/unit --test-selection tests/contracts \
  > "$SCR/mut/gate_docs.log" 2>&1
gate_exit=$?
echo "gate_exit=$gate_exit" >> "$SCR/mut/gate_docs.log"
t3=$(date +%s)
echo "gate_wall_s=$((t3-t2))" >> "$SCR/mut/timing.txt"

# 3. Gate verdict restricted to docs.py via --scope-file (no config change).
"$UV" run python scripts/check_mutation_score.py \
  --mutants-root mutants \
  --scope-file "$SCOPE" \
  --output reports/mutation-summary-docs.json \
  --minimum-score 100 \
  > "$SCR/mut/check_docs.log" 2>&1
check_exit=$?
echo "check_exit=$check_exit" >> "$SCR/mut/check_docs.log"
echo "ALL_DONE gate_exit=$gate_exit check_exit=$check_exit" > "$SCR/mut/done.txt"
