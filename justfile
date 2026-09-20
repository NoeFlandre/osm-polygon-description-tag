set shell := ["bash", "-euo", "pipefail", "-c"]

sync:
    uv sync --frozen

format:
    uv run ruff format .

lint:
    uv run ruff format --check .
    uv run ruff check .

typecheck:
    uv run ty check

test:
    uv run pytest --cov=osm_polygon_description_tag --cov-branch --cov-report=term-missing --cov-fail-under=90

test-integration:
    uv run pytest tests/integration -q

# The suite is the expensive part and `quality` already runs it with coverage,
# so re-running it here only produced the same numbers a second time.
#
# Build the CRAP report from an existing reports/coverage.json.
risk-prepared:
    test -f reports/coverage.json
    uv run radon cc src/osm_polygon_description_tag -s -j > reports/radon.json
    uv run python scripts/quality_metrics.py crap \
        --coverage-json reports/coverage.json \
        --radon-json reports/radon.json \
        --output reports/crap.json \
        --markdown-output reports/crap.md
    uv run python scripts/quality_metrics.py check \
        --report reports/crap.json \
        --max-crap-score 6

# Generate deterministic CRAP risk reports from test coverage and Radon.
risk:
    mkdir -p reports
    uv run pytest --cov=osm_polygon_description_tag --cov-branch --cov-report=json:reports/coverage.json --cov-fail-under=90
    uv run radon cc src/osm_polygon_description_tag -s -j > reports/radon.json
    uv run python scripts/quality_metrics.py crap \
        --coverage-json reports/coverage.json \
        --radon-json reports/radon.json \
        --output reports/crap.json \
        --markdown-output reports/crap.md
    uv run python scripts/quality_metrics.py check \
        --report reports/crap.json \
        --max-crap-score 6

# Record which tests execute which source lines. The mutation gate turns this
# into the exact covering-test set per function, which is what keeps it fast:
# a test that never runs a function's lines cannot kill that function's mutants.
# Keep TMPDIR inherited from the caller; it must remain outside the repository.
# Record which tests cover which functions. Every mutation shard waits on this,
# so it runs across processes: measured 5m04s to 2m37s on eight workers.
#
# The parallel map is not merely the same map sooner. Each worker starts with
# fresh module state, so a lazily imported name that one process resolves once
# and caches is re-resolved in the others, and four ``__getattr__``-style
# functions gain covering tests that a single process never records. Compared
# directly: no function lost a test, four gained some. That shadowing is the
# same effect that let a ``__getattr__`` mutant survive a test reading an
# already-cached attribute, so the parallel map is the sounder one.
mutation-contexts:
    mkdir -p data-root/.tmp
    COVERAGE_FILE="$PWD/data-root/.tmp/.coverage-ctx" \
        uv run pytest -q -p no:cacheprovider -n auto \
        --cov=osm_polygon_description_tag --cov-branch --cov-context=test --cov-report=

# Run the all-source mutation gate for all source modules; mutmut resumes from its ignored cache.
mutation: mutation-contexts
    mkdir -p reports data-root/.tmp
    uv run python -m scripts.run_mutation_gate --max-children 8 \
        --coverage-file data-root/.tmp/.coverage-ctx
    uv run python scripts/check_mutation_score.py \
        --mutants-root mutants \
        --output reports/mutation-summary.json \
        --minimum-score 100

# Run the all-source mutation gate over one shard of the source tree. Sharding
# only splits which modules are mutated; each shard still requires a 100% score,
# so the shards together are exactly the all-source gate. This keeps the main
# push gate inside a sane CI wall-clock budget instead of being cancelled.
#
# ``mutate_file`` also carries the canary module that mutmut's forced-fail probe
# needs, so it is a superset of ``scope_file``. Scoring uses ``scope_file`` only,
# which keeps the shards a clean partition: the canary is scored by the one
# shard that owns it.
# Contexts are a pure function of the source and the test suite, so regenerating
# them inside every shard re-ran the whole suite once per shard for an identical
# result.
#
# Score one shard against coverage contexts that already exist.
mutation-shard-prepared scope_file mutate_file:
    test -f "{{scope_file}}"
    test -f "{{mutate_file}}"
    test -f data-root/.tmp/.coverage-ctx
    mkdir -p reports
    uv run python -m scripts.run_mutation_gate --max-children 8 \
        --coverage-file data-root/.tmp/.coverage-ctx \
        --only-mutate-file "{{mutate_file}}"
    uv run python scripts/check_mutation_score.py \
        --mutants-root mutants \
        --scope-file "{{scope_file}}" \
        --output reports/mutation-summary.json \
        --minimum-score 100

mutation-shard scope_file mutate_file: mutation-contexts
    test -f "{{scope_file}}"
    test -f "{{mutate_file}}"
    mkdir -p reports data-root/.tmp
    uv run python -m scripts.run_mutation_gate --max-children 8 \
        --coverage-file data-root/.tmp/.coverage-ctx \
        --only-mutate-file "{{mutate_file}}"
    uv run python scripts/check_mutation_score.py \
        --mutants-root mutants \
        --scope-file "{{scope_file}}" \
        --output reports/mutation-summary.json \
        --minimum-score 100

# Run a strict mutation gate for the exact source lines and tests changed by a
# PR. The scoped runner uses only the targeted test set; an empty test file
# falls back to the configured repository-wide test root.
# Selection comes from the coverage map, not from which test files the branch
# happens to touch. Measured on a pull request that changed 69 test files and
# 303 source functions: selecting by changed test files runs 2,047 tests for
# every mutant (620,241 test-executions for one mutant each), while the
# coverage map runs the tests that actually cover the mutated function -- a
# median of 32, 20,745 in total. Recording the map costs 2m37s once.
#
# It is also the sounder selection: a mutant killable only by a test the branch
# did not touch is reported as a survivor under the file-based rule.
mutation-scope scope_file: mutation-contexts
    test -f "{{scope_file}}"
    mkdir -p reports data-root/.tmp
    uv run python -m scripts.run_mutation_gate --max-children 8 \
        --coverage-file data-root/.tmp/.coverage-ctx \
        --changed-lines-file "{{scope_file}}"
    uv run python scripts/check_mutation_score.py \
        --mutants-root mutants \
        --output reports/mutation-summary.json \
        --minimum-score 100

# Compute and validate the dataset card and statistics report without uploading.
release-stats-dry-run:
    uv run osm-polygon-description-tag release-stats \
        --confirm-repo NoeFlandre/osm-polygon-description-tag

# Compute, validate, publish, and verify only the card and statistics report.
release-stats:
    uv run osm-polygon-description-tag release-stats \
        --confirm-repo NoeFlandre/osm-polygon-description-tag --apply

build:
    uv build

# Build the minimal non-root runtime image; this does not touch data.
docker-build:
    docker build --target runtime --tag osm-polygon-description-tag:local .

# Run the harmless CLI help command in the runtime image.
docker-help: docker-build
    docker run --rm osm-polygon-description-tag:local --help

# Run the test suite in the development image.
docker-test:
    docker build --target development --tag osm-polygon-description-tag:dev .
    docker run --rm osm-polygon-description-tag:dev

# Run the complete quality suite in the development image.
docker-check:
    docker build --target development --tag osm-polygon-description-tag:dev .
    docker run --rm osm-polygon-description-tag:dev bash -lc \
        'uv run pytest -q && uv run ruff format --check . && uv run ruff check . && uv run ty check'

# Run the stoppable, resumable workflow; raw input is read-only and state stays under data_root.
docker-run data_root: docker-build
    docker run --rm -it \
        --user "$(id -u):$(id -g)" \
        --env HOME=/tmp \
        --mount "type=bind,src={{data_root}},dst=/data" \
        --mount "type=bind,src={{data_root}}/raw,dst=/data/raw,readonly" \
        --env HF_TOKEN \
        osm-polygon-description-tag:local \
        run-and-publish \
        --source-root /data/raw \
        --data-root /data \
        --confirm-repo NoeFlandre/osm-polygon-description-tag

check:
    uv lock --check
    uv run pre-commit run --all-files
    uv run ruff format --check .
    uv run ruff check .
    uv run ty check
    uv run pytest --cov=osm_polygon_description_tag --cov-branch --cov-report=term-missing --cov-fail-under=90
    uv build

run-and-publish:
    uv run osm-polygon-description-tag run-and-publish \
      --source-root "/Volumes/Seagate M3/projects/osm-polygon-wikidata-only/raw" \
      --data-root "/Volumes/Seagate M3/projects/osm-polygon-description-tag/data-root" \
      --confirm-repo NoeFlandre/osm-polygon-description-tag
