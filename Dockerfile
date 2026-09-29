# syntax=docker/dockerfile:1.7
#
# The uv binary is version-pinned. Python and Debian come from the official
# 3.12 Bookworm slim base. Dependency versions are resolved by uv.lock.
# Operators may override UV_IMAGE with a verified registry digest.

ARG UV_IMAGE=ghcr.io/astral-sh/uv:0.11.16

FROM ${UV_IMAGE} AS uv

FROM python:3.12-slim-bookworm AS base

COPY --from=uv /uv /uvx /usr/local/bin/

ENV UV_LINK_MODE=copy \
    UV_COMPILE_BYTECODE=1 \
    UV_PYTHON_DOWNLOADS=0 \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PYTHONHASHSEED=0 \
    LANG=C.UTF-8 \
    LC_ALL=C.UTF-8

# The production CLI invokes the real osmium-tool binary. Install it in the
# shared base so development and runtime images use the same binary contract.
RUN apt-get update \
    && apt-get install -y --no-install-recommends osmium-tool \
    && rm -rf /var/lib/apt/lists/*

FROM base AS build

WORKDIR /app

# Resolve dependencies before copying source files so code-only changes reuse
# the locked dependency layer.
COPY pyproject.toml uv.lock README.md LICENSE ./
RUN uv sync --frozen --no-dev --no-install-project

COPY src ./src
RUN uv sync --frozen --no-dev --no-editable

# Development contains the checkout and dev dependencies; runtime receives
# only the non-editable installed package and its runtime environment.
FROM build AS development

COPY . .
RUN uv sync --frozen

RUN groupadd --system app && useradd --system --gid app --create-home app \
    && chown -R app:app /app
USER app
ENV PATH=/app/.venv/bin:$PATH \
    HOME=/tmp \
    OSM_POLYGON_DESCRIPTION_TAG_HOME=/app
WORKDIR /app
CMD ["uv", "run", "pytest", "-q"]

FROM base AS runtime

LABEL org.opencontainers.image.title="osm-polygon-description-tag" \
      org.opencontainers.image.description="Pipeline for the OSM polygons-with-descriptions dataset" \
      org.opencontainers.image.source="https://github.com/NoeFlandre/osm-polygon-description-tag" \
      org.opencontainers.image.licenses="Apache-2.0"

WORKDIR /app
RUN groupadd --system app && useradd --system --gid app --create-home app
COPY --from=build --chown=app:app /app/.venv /app/.venv

# The roots every command defaults to inside the container: PBFs are read from
# /source (mount it read-only, outside the data root) and everything generated goes under /data.
ENV PATH=/app/.venv/bin:$PATH \
    HOME=/tmp \
    OSM_POLYGON_SOURCE_ROOT=/source \
    OSM_POLYGON_DATA_ROOT=/data

# The host-mounted data root contains raw PBFs, resumable state, logs, and
# generated artifacts. Nothing from those roots is copied into the image.
VOLUME ["/data"]
USER app

# A plain `docker run IMAGE` is a harmless help command. Pass the explicit
# run-and-publish command to opt into data processing and Hub publication.
ENTRYPOINT ["osm-polygon-description-tag"]
CMD ["--help"]
