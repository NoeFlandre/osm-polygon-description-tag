# Changelog

All notable changes to this project are documented here. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and versions follow
[Semantic Versioning](https://semver.org/spec/v2.0.0.html). The version is
defined once, in `pyproject.toml`; `CITATION.cff` must match it.

## [Unreleased]

### Fixed

- Private `typer._click` import isolated; the declared `typer` floor now works (#71).
- Preflight timeouts and OS errors are reported as `PreflightError` (#70).

### Changed

- The CLI imports matplotlib only when a chart is drawn (#84).
- Each geometry is oriented once per record during transform (#85).
- H3 density counts rank unique rows in one pass (#82).

### Quality

- CI smoke-tests the built wheel in a clean venv (#74).
- The CRAP gate scores closures and reports unmatched coverage (#76).
- `scripts/` is measured by coverage, the CRAP gate (#77) and `ty` (#72).
- Deterministic Hypothesis profiles for CI and mutation testing (#78).
- Single-sourced version, this changelog and a tag-driven release workflow (#73).

## [0.1.0]

- First public version.
