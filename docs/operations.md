# Operations

## Storage boundaries

Keep code and data separate:

| Purpose | Path | Rule |
| --- | --- | --- |
| Code checkout | `/Users/noeflandre/osm-polygon-description-tag` | Git-managed source |
| Immutable raw PBFs | `/Volumes/Seagate M3/projects/osm-polygon-wikidata-only/raw` | Read-only; never an output or temp directory |
| Generated artifacts | `/Volumes/Seagate M3/projects/osm-polygon-description-tag` | Parquet, manifests, stats, logs, and local state |

Local state under the data root is explicitly separated:

- `.cache/huggingface/` contains resumable uploader state and verification
  downloads.
- `.work/` contains validation SQLite and DuckDB spill files.
- `logs/` contains the rotated redacted JSONL event stream.
- `publication-state.json` records only verified publication transitions.

None of these local-state paths enters an upload plan.

## Lifecycle

`run-and-publish` performs:

1. deterministic PBF discovery;
2. read-only preflight, including real `osmium`, `hf`, authentication, and Hub
   write-permission checks;
3. build or validated local reuse for each source;
4. atomic Parquet and manifest promotion;
5. an exact five-file per-PBF upload plan (Parquet, manifest, README, stats,
   and `assets/description_polygon_density.png`) and remote verification;
6. atomic publication-state update, including the H3 map identity;
7. deterministic final `README.md` and `stats.json` generation and independent
   metadata publication, including the regenerated H3 map asset;
8. atomic record of publication state with map SHA-256, size, and verified
   revision.

Preflight fails before opening a PBF or creating generated artifacts. Once it
passes, every state transition is persisted only after the relevant artifact
or remote identity is verified.

## Stop and resume

Press Ctrl-C once and wait for the terminal prompt. The CLI returns exit code
130, terminates the active osmium child safely, removes owned incomplete
temporaries, and preserves finalized Parquet, manifests, and publication
state. Rerun the same command:

```bash
just run-and-publish
```

The orchestrator classifies each source as `build`, `reuse-local`, or
`already-published`. It never rebuilds a verified local artifact whose source,
schema, transform, area-policy, and output identities still agree.

## Logs and diagnostics

Events are written to:

```text
/Volumes/Seagate M3/projects/osm-polygon-description-tag/logs/run-and-publish.jsonl
```

The active log rotates at 10 MiB with five backups using same-directory atomic
operations. Human progress is stderr-only; stdout remains one machine-readable
JSON report. Logs are redacted, allowlisted, flushed, and never published.

## Hugging Face safety

The target dataset is `NoeFlandre/osm-polygon-description-tag`. Every upload is
an explicit allowlisted plan. Per-source plans contain exactly the Parquet,
manifest, README, stats, and H3 density map needed for that source. Final
metadata is uploaded separately as exactly `README.md`, `stats.json`, and
`assets/description_polygon_density.png`.

The map asset is regenerated from the complete validated local dataset on
every run and is included in every per-PBF plan and in the final metadata
plan. A change to the map bytes invalidates the metadata no-op path and
forces a fresh metadata upload. An unchanged map preserves the true no-op
metadata publication path.

Remote reconciliation removes only stale files below managed `data/` and
`manifests/` namespaces. Unrelated repository files are preserved. The
pipeline never uploads logs, caches, temporary files, publication state, or
other PBF artifacts.
