# Operations

## Storage boundaries

Keep code and data separate:

| Purpose | Path | Rule |
| --- | --- | --- |
| Code checkout | `/Volumes/Seagate M3/projects/osm-polygon-description-tag` | Git-managed source |
| Immutable raw PBFs | `/Volumes/Seagate M3/projects/osm-polygon-wikidata-only/raw` | Read-only; never an output or temp directory |
| Generated artifacts | `/Volumes/Seagate M3/projects/osm-polygon-description-tag/data-root` | Parquet, manifests, stats, logs, and local state |

Local state under the data root is explicitly separated:

- `.cache/huggingface/` contains resumable uploader state and verification
  downloads.
- `.work/` contains validation SQLite and DuckDB spill files.
- `logs/` contains the rotated redacted JSONL event stream.
- `logs/trackio/` contains the local Trackio SQLite database; it is synced to
  the public static dashboard and never enters an upload plan.
- `publication-state.json` records only verified publication transitions.

None of these local-state paths enters an upload plan.

## Lifecycle

`run-and-publish` performs:

1. deterministic PBF discovery;
2. read-only preflight, including real `osmium`, `hf`, authentication, and Hub
   write-permission checks;
3. build or validated local reuse for each source;
4. global `(osm_type, osm_id)` deduplication with atomic resumable promotion;
5. deterministic README, stats, and visual-asset refresh;
6. an exact seven-file per-PBF upload plan (Parquet, manifest, README, stats,
   H3 map, area histogram, and dataset-card hero) and remote verification;
7. atomic publication-state update, including the H3 map identity;
8. deterministic final `README.md` and `stats.json` generation and independent
   metadata publication, including all three visual assets;
9. atomic record of publication state with map SHA-256, size, and verified
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

If interrupted during deduplication, the staged canonical files remain under
`.work/dedup/` and the next invocation finishes promotion before continuing to
publication, provided the current inputs still match the recorded identities
(or the expected hashes of files already promoted from that stage). Input drift
is refused and the staged state is preserved for safe recovery. A completed
deduplication state is reused when all input output identities and the policy
hash still match.

## Legacy text repair

Artifacts built before the successful-text contract stored description values
exactly as OpenStreetMap held them, including leading and trailing whitespace.
The final-artifact contract requires the canonical trimmed form, so
`publish-plan` refuses such an artifact with `description text must be
trimmed`.

`migrate-text` repairs them in place without reading raw PBFs:

```bash
uv run osm-polygon-description-tag migrate-text
```

It applies the same normalization the current build path applies, so a
migrated artifact matches what a rebuild would produce for this defect. A
value that is only whitespace carries no text and is dropped; a row left
without any description text is excluded and recorded under the existing
`no_nonempty_description` reason. Each Parquet is promoted before its manifest
is updated, so an interrupted run resumes safely, and an artifact that is
already canonical is left byte-identical.

Run `generate-card` afterwards so `stats.json` and the card reflect the
repaired rows.

## Logs and diagnostics

Events are written to:

```text
/Volumes/Seagate M3/projects/osm-polygon-description-tag/data-root/logs/run-and-publish.jsonl
```

The active log rotates at 10 MiB with five backups using same-directory atomic
operations. Human progress is stderr-only; stdout remains one machine-readable
JSON report. Logs are redacted, allowlisted, flushed, and never published.

## Hugging Face safety

The target dataset is `NoeFlandre/osm-polygon-description-tag`. Every upload is
an explicit allowlisted plan. Final metadata is uploaded separately as exactly five files:
`README.md`, `stats.json`,
`assets/description_polygon_density.png`, `assets/area_distribution.png`, and
`assets/dataset-card-hero.png`.

The H3 map is keyed by the identities of the complete validated local Parquet
set. It is recomputed only when that dataset input identity changes. README-only
or stats-only changes reuse the existing PNG bytes and preserve the true no-op
metadata publication path when the allowlisted metadata is unchanged.

Trackio is local-first: metrics are written under the generated-data root while
the pipeline runs, and the completed database is synchronized to the public
static dashboard. A Trackio outage never interrupts extraction or publication.

Remote reconciliation removes only stale files below managed `data/` and
`manifests/` namespaces. Unrelated repository files are preserved. The
pipeline never uploads logs, caches, temporary files, publication state, or
other PBF artifacts.
