# Operations

## Storage boundaries

Keep the code and the data separate:

| Purpose | Set with | Rule |
| --- | --- | --- |
| Code checkout | your `git clone` | Git-managed source |
| Immutable raw PBFs | `--source-root` or `OSM_POLYGON_SOURCE_ROOT` | Read-only. Never use it as an output or temp directory |
| Generated artifacts | `--data-root` or `OSM_POLYGON_DATA_ROOT` | Parquet, manifests, stats, logs, and local state |

The two roots must be disjoint. Neither root can contain the other.

### Maintainer setup (example)

The maintainer's machine uses these paths. They are for reference only:

- Code: `/Volumes/Seagate M3/projects/osm-polygon-description-tag`
- Raw PBFs: `/Volumes/Seagate M3/projects/osm-polygon-wikidata-only/raw`
- Data root: `/Volumes/Seagate M3/projects/osm-polygon-description-tag/data-root`

The local state under the data root is separated explicitly:

- `.cache/huggingface/` contains the resumable uploader state and the
  verification downloads.
- `.work/` contains the validation SQLite files and the DuckDB spill files.
- `logs/` contains the rotated redacted JSONL event stream.
- `logs/trackio/` contains the local Trackio SQLite database. The tool syncs it
  to the public static dashboard. It never enters an upload plan.
- `publication-state.json` records only the verified publication transitions.

None of these local-state paths enters an upload plan.

## Lifecycle

`run-and-publish` does these steps:

1. It discovers the PBF files in a deterministic order.
2. It does a read-only preflight. The preflight includes checks of the real
   `osmium`, `hf`, the authentication, and the Hub write permission.
3. It builds each source, or it reuses the validated local artifact.
4. It does the global `(osm_type, osm_id)` deduplication with an atomic
   promotion that you can resume.
5. It refreshes the deterministic README, the stats, and the visual assets.
6. It uploads an exact seven-file plan for each PBF and verifies it remotely.
   The files are the Parquet file, the manifest, the README, the stats, the H3
   map, the area histogram, and the dataset-card hero.
7. It updates the publication state atomically. The update includes the H3 map
   identity.
8. It generates the deterministic final `README.md` and `stats.json`. It
   publishes the metadata independently, with all three visual assets.
9. It records the publication state atomically with the map SHA-256, the size,
   and the verified revision.

The preflight fails before the tool opens a PBF or creates generated
artifacts. After the preflight passes, the tool saves a state transition only
after it verifies the relevant artifact or remote identity.

## Stop and resume

Press Ctrl-C one time. Wait for the terminal prompt. The CLI returns exit code
130. It terminates the active osmium child safely. It removes the incomplete
temporary files that it owns. It keeps the finalized Parquet files, the
manifests, and the publication state. Run the same command again:

```bash
just run-and-publish
```

The orchestrator classifies each source as `build`, `reuse-local`, or
`already-published`. It does not rebuild a verified local artifact when the
source, schema, transform, area-policy, and output identities still agree.

If an interruption occurs during deduplication, the staged canonical files stay
under `.work/dedup/`. The next run finishes the promotion before it continues
to publication. This applies if the current inputs still match the recorded
identities, or the expected hashes of the files that the tool promoted from that
stage. If the input changes, the tool refuses to continue. It keeps the staged
state for a safe recovery. The tool reuses a completed deduplication state when
all the input output identities and the policy hash still match.

## Legacy text repair

Artifacts that the tool built before the successful-text contract store the
description values exactly as OpenStreetMap held them. This includes leading
and trailing whitespace. The final-artifact contract needs the canonical
trimmed form. Thus `publish-plan` refuses such an artifact with `description
text must be trimmed`.

`migrate-text` repairs these artifacts in place. It does not read the raw PBF
files:

```bash
uv run osm-polygon-description-tag migrate-text
```

The command applies the same normalization as the current build path. Thus a
migrated artifact is the same as the artifact that a rebuild produces for this
defect. A value that has only whitespace has no text, and the tool drops it. A
row that has no description text is excluded. The tool records it under the
existing `no_nonempty_description` reason. The tool promotes each Parquet file
before it updates its manifest. Thus you can resume an interrupted run safely.
The tool does not change an artifact that is already canonical. The bytes stay
the same.

Afterwards, run `generate-card`. Then `stats.json` and the card show the
repaired rows.

## Logs and diagnostics

The tool writes the events to:

```text
<data-root>/logs/run-and-publish.jsonl
```

The active log rotates at 10 MiB with five backups. It uses atomic operations
in the same directory. The human progress goes only to stderr. Stdout stays one
machine-readable JSON report. The logs are redacted, allowlisted, and flushed.
The tool never publishes them.

## Hugging Face safety

The target dataset is `NoeFlandre/osm-polygon-description-tag`. Each upload is
an explicit allowlisted plan. The tool uploads the final metadata separately as
exactly five files: `README.md`, `stats.json`,
`assets/description_polygon_density.png`, `assets/area_distribution.png`, and
`assets/dataset-card-hero.png`.

The identities of the complete validated local Parquet set are the key of the
H3 map. The tool computes the map again only when that dataset input identity
changes. If only the README or only the stats change, the tool reuses the
existing PNG bytes. The metadata publication stays a true no-op when the
allowlisted metadata does not change.

Trackio is local-first. The tool writes the metrics under the generated-data
root while the pipeline runs. It synchronizes the completed database to the
public static dashboard. A Trackio outage does not stop the extraction or the
publication.

Remote reconciliation removes only the stale files below the managed `data/`
and `manifests/` namespaces. It keeps the unrelated repository files. The
pipeline never uploads logs, caches, temporary files, the publication state, or
other PBF artifacts.
