# Shared SaT capabilities

Description owns the shared reference at
`src/osm_polygon_description_tag/_data/sat-capabilities.json` in
[NoeFlandre/osm-polygon-description-tag](https://github.com/NoeFlandre/osm-polygon-description-tag).
Website and Wikidata keep byte-identical copies in their sentence-processing packages.
Each wheel includes its copy. Runtime code reads only that copy. It does not fetch a
reference, import another consumer, or require a model download.

## Version 1

The reference identifies `segment-any-text/sat-3l-sm` at model revision
`137da054051ad9f1eac42025f758db4ac9f22535`. Its version is `sat-3l-sm-v1`.
It records the same 85 supported language codes used by all three consumers before
this change. The `source_url` points to the model card at that revision.

The embedded `digest` is SHA-256 of the JSON object without `digest`, serialized
with sorted keys, compact separators, and UTF-8 encoding. The local loader also
pins SHA-256 of the complete reference file, including whitespace and the embedded
digest. A changed byte fails closed until the loader pin is deliberately updated.

The reference describes model capabilities. It does not change runtime model pins.
Description retains its full model revision and existing runtime library version.
Wikidata retains `137da05`. Website retains its operator-supplied model revision.
This distinction preserves existing checkpoints and release provenance.

## Consumer rules stay local

- Description keeps its ISO 639-3 aliases and explicit unsplit policy. Its language
  fingerprint remains `afdd2bd7aaf106d61b7a692ac0742ddedf33457eb48f24410f5081ac7062809b`.
- Website keeps GlotLID parsing and its additional aliases: `ckb` to `ku`, `khk` to
  `mn`, `pbt` to `ps`, `ydd` to `yi`, and `yue` to `zh`. The independent wtpsplit
  snapshot and locked-version checks remain in place.
- Wikidata accepts exact article-language codes only. Variants and unsupported
  languages remain unsplit.

No published data, historical fingerprints, model locks, or dataset artifacts are
rewritten by this refactor. Independent tests preserve the expected codes and
consumer-specific behavior. Ordinary CI verifies the local digest and runtime use.

## Check for cross-repository drift

From a Description checkout, compare three local checkouts without network access:

```bash
python scripts/check_sat_capabilities.py \
  --description /path/to/description \
  --website /path/to/website \
  --wikidata /path/to/wikidata
```

Omit checkout paths to read the public GitHub default branches. During coordinated
review, use `--description-ref`, `--website-ref`, and `--wikidata-ref` with the exact
review commits. The audit prints each consumer's version and full-file digest.
It exits nonzero on drift, corruption, or a missing reference. It never changes files.
Description also runs this audit through its manual/weekly capability-drift workflow.
The audit is separate from runtime processing and offline unit tests.

## Update procedure

1. Review the model capability evidence and create a new reference version in
   Description. Do not change a version's meaning without changing its version ID.
2. Recompute the embedded payload digest and complete-file pin. Update independent
   expected-value tests only after checking the upstream evidence.
3. Copy the exact reference bytes to Website and Wikidata in linked, reviewed PRs.
   Keep each consumer's aliases and fallback rules local. Any policy change needs
   separate review; do not infer new aliases from a capability update.
4. Run all three offline suites, wheel checks, and the cross-repository audit against
   the review commits. Repeat the audit on main after the coordinated merges.
5. Preserve old release provenance and historical fingerprints. Do not regenerate or
   relabel published datasets as part of a capability-reference update.
