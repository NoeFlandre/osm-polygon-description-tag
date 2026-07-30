# Publication package

## Purpose
Plan, execute, verify, and record guarded Hugging Face publication.

## Public API
The package root preserves the supported legacy import path.

## Models
`models.py` owns immutable plans, items, errors, and retry constants.

## Planning
`planning.py` validates the allowlist and builds exact upload plans and commands.

## Upload
`upload.py` verifies identities and executes bounded, retry-aware uploads.

## Verification
`verification.py` authenticates and verifies exact remote file identities.

## State
`state.py` atomically reads and writes resumable publication state.

## Safety
Plans reject symlinks, temporary files, unknown paths, stale manifests, and identity drift.

## Testing
Hermetic tests live under `tests/unit/publication`; no network or real PBF is required.
