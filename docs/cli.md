# CLI reference

The executable is:

```bash
uv run osm-polygon-description-tag COMMAND
```

Use `--help` on the executable or on a command to see the exact current
options. Each option has a description there.

## Global options

Put these options before the command. For example:
`osm-polygon-description-tag -q run-and-publish ...`

| Option | Effect |
| --- | --- |
| `--version` | Prints the package version and exits with code 0. |
| `-v`, `--verbose` | Also prints DEBUG event lines on stderr, such as `resolved_config` (the roots, osmium, and target repo in use). |
| `-q`, `--quiet` | Prints only WARNING and ERROR event lines on stderr. |

You cannot use `-v` and `-q` together. They change only the human-readable
stderr lines. The JSONL log under `logs/` always records each event. Stdout
does not change.

## Exit codes

| Code | Meaning |
| --- | --- |
| `0` | Success. |
| `1` | Any other failure (I/O, osmium export, orchestration, migration). |
| `2` | Usage error: unknown command or option, bad value. |
| `3` | Environment or configuration: the preflight failed, or a root is missing or unsafe. |
| `4` | Validation failed: manifest, storage, or statistics checks. |
| `5` | Publication failed: upload plan mismatch, upload, or Hub verification. |
| `130` | Interrupted with Ctrl-C. |

An error prints one line on stderr. It never prints a traceback.

## Read-only and local commands

| Command | Purpose | Main side effect |
| --- | --- | --- |
| `inspect` | Discovers the direct source PBFs | Read-only |
| `build-one NAME` | Builds one named source | Writes one Parquet file and manifest under the data root |
| `build-all` | Builds all discovered sources | Writes validated local artifacts |
| `validate` | Validates the Parquet files and manifests | Read-only, except for bounded local work files |
| `generate-card` | Computes `stats.json` and `README.md` again | Writes the changed metadata atomically |
| `migrate-schema` | Upgrades legacy Arrow-map Parquet files to key/value lists that the Hub can show | Rewrites the existing data files atomically. Never reads raw PBFs |
| `migrate-text` | Repairs legacy untrimmed description text to its canonical form | Rewrites the affected data files and manifests atomically. Never reads raw PBFs |
| `trackio-snapshot` | Logs the completed dataset snapshot to Trackio | Writes local Trackio state and refreshes the public static dashboard |
| `publish-plan` | Shows the exact upload plan identity | Read-only |

`validate` works with only a data root. It checks each manifest source name
against the Parquet rows and output name. To also compare recorded source size,
modification time, and SHA-256 with the original PBFs, provide
`--source-root /path/to/pbfs`.

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

This command is the complete operation. You can stop it and resume it:

```bash
uv run osm-polygon-description-tag run-and-publish \
  --source-root "/path/to/pbfs" \
  --data-root "/path/to/data-root" \
  --confirm-repo NoeFlandre/osm-polygon-description-tag
```

Options:

- `--source-root PATH`: the immutable PBF directory. If you omit it, the tool
  reads `OSM_POLYGON_SOURCE_ROOT`. Only `inspect`, `build-one`, `build-all`,
  and `run-and-publish` need it. The data-only commands ignore it.
- `--data-root PATH`: the generated-data directory. If you omit it, the tool
  reads `OSM_POLYGON_DATA_ROOT`.
- `--osmium NAME`: the executable name or path. The default is `osmium`.
- `--confirm-repo REPO`: the exact target repository. This option is required.

If you give a root in neither way, the tool shows an error (exit code 3). The
error names the option and the variable. There is no machine-specific default.

### `release-stats`

This command is the statistics release wrapper. It validates the complete
published Parquet inventory against its manifests. It computes `stats.json` and
`README.md` again from each valid published row. It publishes only the card,
the report, and the required visual assets. It never touches the source data,
the manifests, or the unrelated Hub files.

```bash
# 1. Dry run: compute, validate, and print the exact plan. No network.
uv run osm-polygon-description-tag release-stats \
  --data-root "/path/to/data-root" \
  --confirm-repo NoeFlandre/osm-polygon-description-tag

# 2. Publish and verify the remote revision.
uv run osm-polygon-description-tag release-stats \
  --data-root "/path/to/data-root" \
  --confirm-repo NoeFlandre/osm-polygon-description-tag --apply
```

Options:

- `--confirm-repo REPO`: required. It must be equal to
  `NoeFlandre/osm-polygon-description-tag`.
- `--apply` / `--dry-run`: `--apply` uploads and verifies. `--dry-run` only
  computes. It is the default.

Before `--apply` uploads data, the command pins the current Hub revision. It
verifies the complete remote `data/` and `manifests/` inventory against the
local Parquet and manifest bytes. It refuses to publish when it cannot verify
the inventory or when the inventory is different from the local one. It
verifies the same inventory again at the resulting metadata commit. This finds
a concurrent data change. The JSON report records these items:

- the preflight `data_revision`;
- the target repository;
- the plan identity;
- the verified metadata revision;
- each published file with its SHA-256 and size;
- the number of validated Parquet files;
- the number of published rows.

The regeneration writes a file only when its bytes change. A second run over
unchanged artifacts is a no-op. It gives the same plan identity.

The equivalent recipes are `just release-stats-dry-run` and `just
release-stats`.

### `publish`

The lower-level publication command needs an exact plan identity that
`publish-plan` generates. It also needs existing authenticated Hugging Face
credentials:

```bash
uv run osm-polygon-description-tag publish --plan PLAN_IDENTITY_SHA256
```

The command does not authenticate. It does not discover sources. It does not
build data again. It does not accept a token argument.

### `trackio-snapshot`

For a completed dataset, this command derives a deterministic cumulative curve
for each Parquet file. It also derives summary metrics. It uses the validated
local artifacts. It stores the Trackio database under the data root. It
synchronizes a public static dashboard:

```bash
uv run osm-polygon-description-tag trackio-snapshot \
  --data-root "/path/to/data-root" \
  --run-name snapshot-2026-07-31
```

`run-and-publish` uses the same recorder. During a live run, it logs the source
points and the aggregate points. After the run, it syncs the completed local
database. The cumulative curves use PBF index steps that the tool sorts by
filename. They never use the elapsed time. The dashboard also has the per-PBF
table, the summary, the ranked regional plots, the H3 map, and the area
histogram.

## `language`

The `language` group produces the additive `language-v1` annotations. Its
default detector is Lingua 2.2.0. The tool uses the pinned GlotLID v3 fallback
only when Lingua is `uncertain`. The [language detection runbook](language-detection.md)
describes the group in full. It includes the limits and the recovery behaviour.

```bash
uv run osm-polygon-description-tag language prepare --source-root <src> --run-dir <run> --project-root .
uv run osm-polygon-description-tag language run --source-root <src> --run-dir <run> --shard region.parquet
uv run osm-polygon-description-tag language validate --run-dir <run>
uv run osm-polygon-description-tag language export --run-dir <run> --export-dir <export>
uv run osm-polygon-description-tag language publish --export-dir <export> --repo <repo> --confirm-repo <repo>
```

`prepare` freezes an immutable input snapshot. `run` uses a bounded processing
budget on one shard and pauses so that you can resume it. `validate` reports
the completeness and is read-only. `export` refuses each run that is not fully
complete. `publish` makes a plan by default. It uploads only with `--apply` and
a matching `--baseline-revision`.

The nested `language grid` group prepares, stages, submits, reconciles, and
collects one tiny Grid'5000 job. `submit` and `status` do not contact a
scheduler unless you give `--apply`. The policy preflight fails closed on
anything that it cannot interpret positively.

```bash
uv run --no-sync osm-polygon-description-tag language grid stage --run-dir <run> --shard region.parquet \
  --project-root <project> --source-root <src> --remote-bundle-dir /home/user/language-bundle
uv run --no-sync osm-polygon-description-tag language grid prepare --run-dir <run> --shard region.parquet \
  --remote-project-dir /home/user/project --remote-source-dir /tmp/src --remote-run-dir /tmp/run
uv run --no-sync osm-polygon-description-tag language grid submit --run-dir <run> --shard region.parquet --site nancy ...
uv run --no-sync osm-polygon-description-tag language grid status --run-dir <run> --shard region.parquet
uv run --no-sync osm-polygon-description-tag language grid collect --run-dir <run> --shard region.parquet
```

`stage` prepares a portable one-shard payload locally. It prints the transfer
plan. `--apply` enables the transfer. A Cascade Grid job needs an explicit
remote path to the pinned GlotLID v3 model. The paths must be visible in the
current filesystem. Normally they are on the shared storage or the frontend of
the site. These commands use an operator environment that is already installed.

WARNING: Do not install dependencies on the frontend.

`prepare` alone writes script metadata. It does not transfer inputs. Both
commands write the initial zero-cursor checkpoint of the shard. Thus you can
collect a job that fails during setup. `stage` also reports each orphan part or
receipt without a checkpoint that it moved aside, under `quarantined`. Refer to
the runbook for these topics: retrieval, terminal-job reconciliation, crash
recovery, and explicit paused-run continuation. This implementation did not
run any Grid'5000 job, OAR command, SSH session, production run, or Hub upload.

## Output and exit codes

A successful command writes one JSON report to stdout. The human diagnostics
and the interactive progress use stderr. The exit codes are:

- `0`: successful operation, including a safe no-op;
- `1`: operational failure;
- `2`: invalid command or option usage;
- `130`: one graceful Ctrl-C interruption.
