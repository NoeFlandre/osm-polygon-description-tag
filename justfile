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
    uv run pytest --cov=osm_polygon_description_tag --cov-report=term-missing --cov-fail-under=90

test-integration:
    uv run pytest tests/integration -q

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
    uv run pytest --cov=osm_polygon_description_tag --cov-report=term-missing --cov-fail-under=90
    uv build

run-and-publish:
    uv run osm-polygon-description-tag run-and-publish \
      --source-root "/Volumes/Seagate M3/projects/osm-polygon-wikidata-only/raw" \
      --data-root "/Volumes/Seagate M3/projects/osm-polygon-description-tag" \
      --confirm-repo NoeFlandre/osm-polygon-description-tag
