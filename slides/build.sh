#!/usr/bin/env bash
# Build both local Colloquium decks.
#
# Usage:
#   ./slides/build.sh                  # writes slides/build/{codebase,dataset}/{codebase,dataset}.html
#   ./slides/build.sh path/to/output   # writes both decks below that directory

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_ROOT="$(cd "${SCRIPT_DIR}/.." && pwd)"
OUTPUT_DIR="${1:-${SCRIPT_DIR}/build}"

if ! command -v colloquium >/dev/null 2>&1; then
    echo "error: colloquium CLI not found. Install with: uv tool install colloquium" >&2
    exit 127
fi

cd "${PROJECT_ROOT}"
rm -rf "${OUTPUT_DIR}"
mkdir -p "${OUTPUT_DIR}"

for deck in codebase dataset; do
    colloquium build "${SCRIPT_DIR}/${deck}.md" -o "${OUTPUT_DIR}/${deck}"
    cp -R "${SCRIPT_DIR}/assets" "${OUTPUT_DIR}/${deck}/assets"
    echo "built: ${OUTPUT_DIR}/${deck}/${deck}.html"
done
