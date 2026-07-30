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
