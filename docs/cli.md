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
| `publish-plan` | Show the exact upload plan identity | Read-only |

Examples:

```bash
uv run osm-polygon-description-tag inspect
uv run osm-polygon-description-tag build-one japan-latest.osm.pbf
uv run osm-polygon-description-tag validate
uv run osm-polygon-description-tag generate-card
uv run osm-polygon-description-tag publish-plan
```

## Publication commands

### `run-and-publish`

The complete stoppable and resumable operation:

```bash
uv run osm-polygon-description-tag run-and-publish \
  --source-root "/Volumes/Seagate M3/projects/osm-polygon-wikidata-only/raw" \
  --data-root "/Volumes/Seagate M3/projects/osm-polygon-description-tag" \
  --confirm-repo NoeFlandre/osm-polygon-description-tag
```

Options:

- `--source-root PATH`: immutable PBF directory; defaults to the approved
  Seagate raw root.
- `--data-root PATH`: generated-data directory; defaults to the approved
  Seagate data root.
- `--osmium NAME`: executable name or path; defaults to `osmium`.
- `--confirm-repo REPO`: required exact target repository confirmation.

### `publish`

The lower-level publication command requires an exact plan identity generated
by `publish-plan` and existing authenticated Hugging Face credentials:

```bash
uv run osm-polygon-description-tag publish --plan PLAN_IDENTITY_SHA256
```

It does not authenticate, discover sources, rebuild data, or accept a token
argument.

## Output and exit codes

Successful commands write one JSON report to stdout. Human diagnostics and
interactive progress use stderr. Exit codes are:

- `0`: successful operation, including a safe no-op;
- `1`: operational failure;
- `2`: invalid command or option usage;
- `130`: one graceful Ctrl-C interruption.
