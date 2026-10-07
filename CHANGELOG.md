# Changelog

This file records all important changes to this project. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/). The versions follow
[Semantic Versioning](https://semver.org/spec/v2.0.0.html). The version is
defined once, in `pyproject.toml`. `CITATION.cff` must have the same version.

## [Unreleased]

- Read SaT model capabilities from a versioned, digest-checked offline reference.
  Preserve sentence-routing policy, runtime model pins, and historical fingerprints.

### Added

- `language grid run`: the multi-shard Grid'5000 driver is now part of the installed CLI. It was `scripts/run_language_grid.py` (#56).
- Different exit codes: 3 for environment, 4 for validation, 5 for publication (#56).
- `--version`, and `-v`/`-q`. They show DEBUG events, or only warnings and errors, on stderr. Each CLI option has help text. The main commands show an example (#55).

### Fixed

- The run log keeps events written after the log is closed or after a failed rotation. It no longer holds them in memory where they are never flushed (#159).
- `build-one` exits with code 3 when the requested source is not discovered.
  `validate` exits with code 4 when its data directory is missing (#135).
- Resolve exact 180° longitude ties consistently during antimeridian clipping (#113).
- Canonical selection puts null versions and timestamps last, matching DuckDB, and rejects malformed nonempty timestamps.
- The uploader retries on timeout messages in stderr. It keeps live output and a bounded error tail (#105).
- The private `typer._click` import is isolated. The declared `typer` minimum version now works (#71).
- The tool reports preflight timeouts and OS errors as `PreflightError` (#70).

### Changed

- The CLI imports matplotlib only when it draws a chart (#84).
- The transform step orients each geometry one time for each record (#85).
- H3 density counts rank unique rows in one pass (#82).
- The roots come from `--source-root`/`--data-root` or `OSM_POLYGON_SOURCE_ROOT`/`OSM_POLYGON_DATA_ROOT`. There are no machine-specific defaults (#69).
- The README and the docs use portable paths. They describe install, usage, outputs, and configuration (#67).

### Quality

- Ruff also enforces C4, PIE, RET, PERF, PLW, ARG, BLE, DTZ, PTH, T20, N, ERA, and PGH (#59).
- CI runs a smoke test on the built wheel in a clean venv (#74).
- The CRAP gate scores closures. It reports unmatched coverage (#76).
- Coverage, the CRAP gate (#77), and `ty` (#72) measure `scripts/`.
- Deterministic Hypothesis profiles for CI and mutation testing (#78).
- One source for the version, this changelog, and a release workflow that a tag starts (#73).

## [0.1.0]

- First public version.
