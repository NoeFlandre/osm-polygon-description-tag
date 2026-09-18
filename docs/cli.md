# CLI reference

The executable is:

```bash
uv run osm-polygon-description-tag COMMAND
```

Use `--help` on the executable or any command for the exact current option
surface.

## Read-only and local commands

| Command | Purpose | Main side effect |
| --- | --- | --- |
| `inspect` | Discover direct source PBFs | Read-only |
| `build-one NAME` | Build one named source | Writes one Parquet and manifest under the data root |
| `build-all` | Build all discovered sources | Writes validated local artifacts |
| `validate` | Validate Parquet files and manifests | Read-only apart from bounded local work files |
| `generate-card` | Recompute `stats.json` and `README.md` | Atomically writes changed metadata |
| `migrate-schema` | Upgrade legacy Arrow-map Parquets to Hub-viewable key/value lists | Atomically rewrites existing data files; never reads raw PBFs |
| `migrate-text` | Repair legacy untrimmed description text to its canonical form | Atomically rewrites affected data files and manifests; never reads raw PBFs |
| `trackio-snapshot` | Log the completed dataset snapshot to Trackio | Writes local Trackio state and refreshes the public static dashboard |
| `publish-plan` | Show the exact upload plan identity | Read-only |

Examples:

```bash
uv run osm-polygon-description-tag inspect
uv run osm-polygon-description-tag build-one japan-latest.osm.pbf
uv run osm-polygon-description-tag validate
uv run osm-polygon-description-tag generate-card
uv run osm-polygon-description-tag migrate-schema
uv run osm-polygon-description-tag migrate-text
uv run osm-polygon-description-tag trackio-snapshot
uv run osm-polygon-description-tag publish-plan
```

## Publication commands

### `run-and-publish`

The complete stoppable and resumable operation:

```bash
uv run osm-polygon-description-tag run-and-publish \
  --source-root "/Volumes/Seagate M3/projects/osm-polygon-wikidata-only/raw" \
  --data-root "/Volumes/Seagate M3/projects/osm-polygon-description-tag/data-root" \
  --confirm-repo NoeFlandre/osm-polygon-description-tag
```

Options:

- `--source-root PATH`: immutable PBF directory; defaults to the approved
  Seagate raw root.
- `--data-root PATH`: generated-data directory; defaults to the approved
  Seagate data root.
- `--osmium NAME`: executable name or path; defaults to `osmium`.
- `--confirm-repo REPO`: required exact target repository confirmation.

### `release-stats`

The statistics release wrapper. It validates the complete published Parquet
inventory against its manifests, recomputes `stats.json` and `README.md` from
every valid published row, and publishes only the card, the report, and the
required visual assets. Source data, manifests, and unrelated Hub files are
never touched.

```bash
# 1. Dry run: compute, validate, and print the exact plan. No network.
uv run osm-polygon-description-tag release-stats \
  --data-root "/Volumes/Seagate M3/projects/osm-polygon-description-tag/data-root" \
  --confirm-repo NoeFlandre/osm-polygon-description-tag

# 2. Publish and verify the remote revision.
uv run osm-polygon-description-tag release-stats \
  --data-root "/Volumes/Seagate M3/projects/osm-polygon-description-tag/data-root" \
  --confirm-repo NoeFlandre/osm-polygon-description-tag --apply
```

Options:

- `--confirm-repo REPO`: required; must equal
  `NoeFlandre/osm-polygon-description-tag`.
- `--apply` / `--dry-run`: upload and verify, or compute only (the default).

Before `--apply` uploads anything, the command pins the current Hub revision
and verifies the complete remote `data/` and `manifests/` inventory against the
local Parquet/manifest bytes. It refuses to publish when that inventory cannot
be verified or differs locally; it verifies the same inventory again at the
resulting metadata commit to catch a concurrent data change. The JSON report
records that preflight `data_revision`, the target repository, the plan
identity, the verified metadata revision, every published file with its
SHA-256 and size, the validated Parquet file count, and the published row
count. Regeneration writes a file only when its bytes change, so a second run
over unchanged artifacts is a no-op that yields the same plan identity.

Equivalent recipes: `just release-stats-dry-run` and `just release-stats`.

### `publish`

The lower-level publication command requires an exact plan identity generated
by `publish-plan` and existing authenticated Hugging Face credentials:

```bash
uv run osm-polygon-description-tag publish --plan PLAN_IDENTITY_SHA256
```

It does not authenticate, discover sources, rebuild data, or accept a token
argument.

### `trackio-snapshot`

For a completed dataset, this command derives a deterministic cumulative
per-Parquet curve and summary metrics from validated local artifacts, stores the
Trackio database under the data root, and synchronizes a public static
dashboard:

```bash
uv run osm-polygon-description-tag trackio-snapshot \
  --data-root "/Volumes/Seagate M3/projects/osm-polygon-description-tag/data-root" \
  --run-name snapshot-2026-07-31
```

The same recorder is used by `run-and-publish`: it logs source and aggregate
points during a live run and syncs the completed local database after the run.
The cumulative curves use PBF index steps sorted by filename, never elapsed
time. The dashboard also contains the per-PBF table, summary, ranked regional
plots, H3 map, and area histogram.

## `language`

The `language` group produces the additive `language-v1` annotations. Its default
detector is Lingua 2.2.0 with the pinned GlotLID v3 fallback used only when
Lingua is `uncertain`. It is documented in full, including limitations and
recovery behaviour, in the [language detection runbook](language-detection.md).

```bash
uv run osm-polygon-description-tag language prepare --source-root <src> --run-dir <run> --project-root .
uv run osm-polygon-description-tag language run --source-root <src> --run-dir <run> --shard region.parquet
uv run osm-polygon-description-tag language validate --run-dir <run>
uv run osm-polygon-description-tag language export --run-dir <run> --export-dir <export>
uv run osm-polygon-description-tag language publish --export-dir <export> --repo <repo> --confirm-repo <repo>
```

`prepare` freezes an immutable input snapshot, `run` spends a bounded
processing budget on one shard and pauses resumably, and `validate` reports
completeness read-only. `export` refuses anything but a fully complete run, and
`publish` plans by default: it uploads only with `--apply` and a matching
`--baseline-revision`.

The nested `language grid` group prepares, stages, submits, reconciles, and collects one
tiny Grid'5000 job. `submit` and `status` contact no scheduler unless `--apply`
is passed, and the policy preflight fails closed on anything it cannot
positively interpret.

```bash
uv run --no-sync osm-polygon-description-tag language grid stage --run-dir <run> --shard region.parquet \
  --project-root <project> --source-root <src> --remote-bundle-dir /home/user/language-bundle
uv run --no-sync osm-polygon-description-tag language grid prepare --run-dir <run> --shard region.parquet \
  --remote-project-dir /home/user/project --remote-source-dir /tmp/src --remote-run-dir /tmp/run
uv run --no-sync osm-polygon-description-tag language grid submit --run-dir <run> --shard region.parquet --site nancy ...
uv run --no-sync osm-polygon-description-tag language grid status --run-dir <run> --shard region.parquet
uv run --no-sync osm-polygon-description-tag language grid collect --run-dir <run> --shard region.parquet
```

`stage` prepares a portable one-shard payload locally and prints the transfer
plan; `--apply` enables the transfer. Cascade Grid jobs require an explicit
remote path to the pinned GlotLID v3 model. The paths must be visible in the current
filesystem, normally on the site's frontend/shared storage. These commands use
an already-installed operator environment; do not install dependencies on the
frontend. `prepare` alone
writes script metadata and does not transfer inputs. Both write the shard's
initial zero-cursor checkpoint, so a job that dies during setup is still
collectable; `stage` also reports any uncheckpointed orphan part or receipt it
moved aside under `quarantined`. See the runbook for retrieval, terminal-job
reconciliation, crash recovery, and explicit paused-run continuation. No
Grid'5000 job, OAR command, SSH session, production run, or Hub upload has been
executed for this implementation.

## Output and exit codes

Successful commands write one JSON report to stdout. Human diagnostics and
interactive progress use stderr. Exit codes are:

- `0`: successful operation, including a safe no-op;
- `1`: operational failure;
- `2`: invalid command or option usage;
- `130`: one graceful Ctrl-C interruption.
