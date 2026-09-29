#!/usr/bin/env bash
# Fail on a known-vulnerable pin in uv.lock (pip-audit against the PyPI advisory data).
#
# Ignored on purpose, each until the owner decides:
#   PYSEC-2026-113  pyarrow 21: use-after-free when reading an Arrow IPC *file* with
#                   pre-buffering. This project reads Parquet only. Moving pyarrow past
#                   22 can change the bytes of written Parquet files, and so the
#                   published hashes, so the upgrade needs a deliberate release.
set -euo pipefail

requirements="$(mktemp)"
trap 'rm -f "$requirements"' EXIT
uv export --frozen --no-hashes --no-emit-project --quiet > "$requirements"
uvx pip-audit==2.10.1 --requirement "$requirements" --disable-pip --no-deps \
    --ignore-vuln PYSEC-2026-113
