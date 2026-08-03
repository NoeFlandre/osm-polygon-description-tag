# Project TODO

Last reviewed: 2026-08-03

This is the live delivery ledger for the public dataset project.

## Remaining delivery work

- [x] Migrate all existing external-root Parquets and manifests from legacy
  Arrow maps to the Hub-viewable key/value-list schema, then upload and verify
  the Dataset Viewer (`train` split and first rows).
- [x] Publish the refreshed deterministic README, stats, H3 map, histogram,
  and presentation links to the dataset card.
- [x] Retain only the `snapshot-2026-07-31` Trackio run locally and remotely;
  remove the accidental second run and verify the public Space.
- [x] Push the removal of `docs/superpowers/`; that private planning material
  must not exist in the GitHub repository.
- [x] Commit and push the completed changes, then confirm one clean `main` worktree and
  branch, and verify GitHub/Hugging Face inventories and hashes.

## Completed foundations

- [x] Resumable, stoppable `run-and-publish` pipeline with bounded memory,
  atomic artifacts, manifests, retries, logs, and per-PBF publication plans.
- [x] Immutable raw-source boundary and Seagate-backed generated-data root.
- [x] GeoParquet polygons and multipolygons with complete tags, names,
  descriptions, WKB geometry, bounding boxes, and geodesic `area_m2`.
- [x] Global `(osm_type, osm_id)` deduplication before final publication.
- [x] Deterministic data-derived stats, dataset card, H3 density map, and
  area-distribution histogram with input-identity caching.
- [x] Trackio snapshot dashboard, dataset and codebase slide decks, MkDocs
  Material documentation, and GitHub Pages publication wiring.
- [x] `uv`, Ruff, `ty`, pytest, pre-commit, Just, and GitHub Actions tooling.

## Acceptance rule

No item is marked complete until the local artifact, commit, and relevant live
remote state have been independently verified.
