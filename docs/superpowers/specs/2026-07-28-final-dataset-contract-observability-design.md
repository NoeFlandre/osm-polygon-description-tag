# Final Dataset Contract and Operational Observability Design

## Purpose and constraints

This amendment closes the remaining dataset-contract and long-run observability gaps while preserving commit `697396f` behavior: exact four-file per-PBF uploads, real Hub verification, write-permission preflight, `.cache/huggingface` resumability, independently resumable metadata, bounded hashing, hermetic tests, and Ctrl-C exit 130.

Implementation and tests use only synthetic OSM fixtures and temporary directories. They must not process any real source under `/Volumes/Seagate M3/projects/osm-polygon-wikidata-only/raw`, write under `/Volumes/Seagate M3/projects/osm-polygon-description-tag`, contact Hugging Face, push Git, or publish anything.

## Closed-way and relation coverage

The export policy uses osmium's documented general area handling rather than a partial polygon-key list:

- `area_tags` is `true` so every tagged closed way is eligible for area creation.
- `linear_tags` is `true`, matching osmium's documented default classification support.
- `osmium export` is called with `--geometry-types polygon`, so nodes, open ways, and line outputs never enter the COPY stream.
- Osmium's explicit `area=no` handling remains authoritative and excludes that closed way from polygon output. `area=yes` remains authoritative for inclusion.
- Osmium assembles `type=multipolygon` and `type=boundary` relations as Polygon or MultiPolygon geometry.
- Python retains only features with at least one exact, non-empty `description` or `description:<suffix>` value.

The packaged and repository-visible osmium configurations remain byte-identical. The changed policy participates in the area-policy checksum. The transform/output algorithm version is bumped so old artifacts cannot be resumed under the new contract.

A committed synthetic fixture assigns distinct IDs to each required case. A real-osmium test converts that XML fixture to PBF in a pytest temporary directory, exercises actual area assembly, runs the normal build path, and asserts exact included and excluded IDs. It also verifies `area=no`, nodes, open ways, and undescribed way/relation records are absent.

## Arrow and row contract

The schema version and transform algorithm version are bumped to 2. The exact field order is:

1. `source_pbf: string non-null`
2. `osm_type: string non-null`
3. `osm_id: int64 non-null`
4. `osm_url: string non-null`
5. `version: int32 nullable`
6. `changeset: int64 nullable`
7. `timestamp: timestamp[ms, UTC] nullable`
8. `name: string nullable`
9. `localized_names: map<string,string> non-null`
10. `description: string nullable`
11. `localized_descriptions: map<string,string> non-null`
12. `tags: map<string,string> non-null`
13. `geometry_type: string non-null`
14. `area_m2: float64 non-null`
15. `bbox_min_x: float64 non-null`
16. `bbox_min_y: float64 non-null`
17. `bbox_max_x: float64 non-null`
18. `bbox_max_y: float64 non-null`
19. `geometry: binary non-null`

`name` contains the exact non-empty `name=*` value, or null when missing or whitespace-only. `localized_names` contains every exact non-empty `name:<suffix>` value keyed by the unmodified non-empty suffix. Suffixes such as `pt-BR` are not normalized or validated. Empty-value checks may trim only for eligibility; stored values remain exact.

The complete `tags` map remains authoritative. Every original tag key and value is preserved exactly, including whitespace, Unicode, punctuation, capitalization, and unusual suffixes. First-class name and description fields are query conveniences and tests assert they agree exactly with `tags` for ways and relations.

Geometry remains complete two-dimensional WKB for Polygon or MultiPolygon. GeoParquet 1.1 metadata identifies the primary geometry and CRS84 longitude/latitude semantics. `area_m2` remains positive WGS84 geodesic area after orientation normalization, with holes subtracted and all multipolygon components included. Bounding-box columns cover the complete geometry.

## Deterministic reporting

Generated dataset artifacts are pure functions of the validated Parquets, matching manifests, and card template. Operational clocks are not serialized into `stats.json` or generated README sections. Existing clock injection may remain as a compatibility hook, but changing it cannot change output bytes.

The stats schema version is bumped. Reporting validates each manifest/output identity before aggregation and computes at least:

- source PBF count and Parquet count;
- total rows;
- rows by OSM type;
- rows by geometry type;
- rows with base descriptions;
- rows with localized descriptions;
- exact description suffix frequencies;
- rows with base names;
- rows with localized names;
- exact name suffix frequencies;
- positive-area count and deterministic minimum, p25, median, p75, and maximum;
- total source and output bytes;
- minimum and maximum non-null OSM data timestamps;
- transformation rejection counts by reason;
- deterministic per-file records containing source filename, Parquet filename, row count, source/output sizes, and source/output SHA-256.

All generated numerical claims are rendered from this stats payload. No handwritten counts appear in generated sections.

README and stats writes compare final UTF-8 bytes before mutation. Identical bytes cause no write and preserve mtimes. Changed bytes use an owned sibling temporary file, file fsync, atomic `os.replace`, and parent-directory fsync. Tests generate with two different clocks, compare bytes and SHA-256, then prove a second identical regeneration does not invalidate metadata state or trigger another metadata upload.

## Explicit operational event sink

A minimal typed event sink is dependency-injected through the orchestrator, build progress, publication retry, verification, and state-write boundaries. It uses no global logging configuration or mutable global context.

Each run receives one UUID run ID and an operational UTC clock. Events use an allowlisted schema with:

- UTC timestamp;
- run ID;
- level;
- event name;
- source index and total when source-scoped;
- source filename when source-scoped;
- event-specific safe scalar fields.

Required events cover preflight, source decision, build start/progress/completion, output rows/bytes, upload start/retry/completion, verification start/completion and revision, atomic state-write completion, metadata skip/upload/verification/state write, final summary, failure, and interruption. Source decisions are exactly `build`, `reuse-local`, or `already-published`. Build progress defaults to every 100,000 emitted features and never logs every row.

The sink writes a concise human-readable line to stderr and a canonical JSON object to `<data-root>/logs/run-and-publish.jsonl`. Both streams flush immediately; the file is also fsynced after each bounded event so lifecycle evidence survives interruption. Stdout remains reserved for the final machine-readable report.

Fields are constructed from allowlisted values rather than arbitrary process environments or full commands. Credential-like text in diagnostic values is redacted before either sink receives it. Tests cover HF-style token strings, bearer values, authorization fields, and token query/assignment forms.

### Preflight and path safety

Terminal events start immediately, but persistent events are held in a small bounded memory buffer until the Hub write-permission preflight succeeds. A denied preflight therefore preserves the existing guarantee that no PBF or generated-data artifact is touched. After success, the sink validates and opens the log location, then persists the buffered events in order.

`logs` must be a real direct child of the data root. The directory, active log, staging names, and backups must not be symlinks. Existing files must be regular files. Fixed internal names prevent traversal. Optional arbitrary log paths are not required by this amendment.

### Bounded atomic rotation

Retention is 10 MiB for the active file plus five backups. Rotation is synchronous and uses only same-directory operations:

1. Flush and fsync the active file.
2. Create and fsync an owned empty replacement with exclusive creation and no symlink following.
3. Snapshot the active file through an owned same-directory hard-link staging name.
4. Shift only the five fixed backup names using atomic `os.replace`, with directory fsync after transitions.
5. Atomically replace backup 1 with the staged snapshot.
6. Atomically replace the active path with the prepared empty file and fsync the directory.

The canonical active path remains valid until the final swap. Interruption may leave an owned staging file or a gap in backup numbering, but never a partial/truncated active or archive. Startup validates names, removes only its own stale staging files after proving they are regular non-symlinks, and safely resumes rotation. Tests inject interruption at rotation stages and verify recovery, content integrity, and retention bounds.

## Upload isolation

`logs/` is accepted locally only after its path safety checks pass. It never produces an `UploadItem`. The canonical per-PBF builder remains exactly four files: the current Parquet, its manifest, `README.md`, and `stats.json`. The final metadata builder remains exactly README and stats.

Tests prove that `logs/`, `.cache/`, `publication-state.json`, temporary files, prior/other PBF artifacts, and rotated log backups never appear in a plan or `--include` argument. Symlinked logs paths fail closed before upload planning. Operational logging never mutates Parquets, manifests, README, stats, cache, or publication state.

## Public CLI lifecycle

The exact existing command remains sufficient and enables terminal plus persistent logging by default. A public `--progress-interval` option may be added for testability and operator tuning, with a positive default of 100,000. No shell piping is needed.

The source loop keeps deterministic filename order. Every source decision is logged before action. Existing publication state semantics remain unchanged:

- `already-published` performs no build, upload, verifier call, or state write;
- `reuse-local` uploads a validated resumable local artifact without rebuilding;
- `build` uses real osmium and atomic GeoParquet/manifest promotion before upload;
- state is written only after Hub verification;
- final metadata is independently skipped or uploaded based on verified metadata identity.

KeyboardInterrupt is logged with the current lifecycle stage and source context, then propagates to the public CLI, which returns the shared exit code 130. Prior complete artifacts and state remain intact.

## Three-run integration proof

A public-entry-point integration test uses three deterministic synthetic PBFs assembled by the installed osmium binary. Only HF preflight/API, upload subprocess, and Hub verification boundaries are replaced with recording fakes.

Run 1 builds and publishes the first source. The fake upload boundary interrupts during the second source after its real osmium build completes. The CLI exits 130. The first source's state is valid, the second source's local Parquet and manifest are valid but unpublished, later sources are untouched, and logs contain the full lifecycle plus interruption.

Run 2 records `already-published` for the first source, `reuse-local` for the interrupted source, builds remaining sources, verifies each upload, generates deterministic final metadata, verifies it, writes metadata state, and prints a successful report.

Run 3 records every source as `already-published`, skips metadata, performs zero builds, dataset uploads, Hub verifier calls, or dataset/state writes, and emits a no-op run summary. Only the operational JSONL log may append or rotate.

The test snapshots hashes, bytes, and relevant mtimes for every Parquet, manifest, README, stats file, and publication-state file. It also asserts every upload include list exactly.

## TDD and verification

Implementation proceeds in focused RED/GREEN cycles:

1. real-osmium inclusion contract;
2. name schema and transformations;
3. deterministic docs and expanded stats;
4. event sink, safety, redaction, rotation, and recovery;
5. orchestrator event integration and retry visibility;
6. public CLI three-run lifecycle and upload exclusions;
7. public README and generated card contract.

RED evidence consists of targeted failures observed before production edits. GREEN evidence consists of those focused tests plus fresh project-wide Ruff, format, mypy, pytest with at least 90% coverage and zero skips, `git diff --check`, and clean Git status. The handoff records exact schema, real-osmium IDs, deterministic hashes across clocks, lifecycle counters, representative log events, upload exclusions, and external-directory integrity evidence.

The work ends after a clean local commit. No real-data run, Hugging Face contact, interactive authentication, push, or publication is authorized.
