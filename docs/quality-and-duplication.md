# Code quality and duplication review

The quality workflow checks every reported function against a strict CRAP
score limit of less than 6. A score of exactly 6 fails. The check has no
allowlist; run `just risk` after tests to refresh the full coverage and CRAP
reports.

The production and script review consolidated shared implementations for
numeric coercion, in-memory DuckDB setup, geography count labels, and report
file writing. Risk report construction now lives in one cohesive module,
separate from its command-line interface. Repeated CLI, publication,
orchestration, and Grid test inputs also use shared domain helpers while
individual tests retain their own names and assertions.

An AST function-body scan over Python files in `src`, `scripts`, and `tests`
ignored a leading docstring, `pass` or ellipsis-only bodies, and bodies shorter
than four nonblank lines. The scan found one remaining exact production clone:
the two lazy package-level `__getattr__` implementations in
`dataset/__init__.py` and `dataset/languages/__init__.py`. Each resolves its
own export map, imports on first use, and caches the result. They stay local so
each package keeps its independent lazy-import boundary; routing them through
a shared helper would leave two thin wrappers around the same small body.
The same scan found no remaining cross-file test function clones at that
threshold.

The source-name-derived fake exporter is shared by orchestrator tests. The
timeout propagation test keeps its separate fake exporter because it uses a
fixed OSM identifier and keeps its imports inside the callback. Those details
are part of that fixture's test contract. Similar test flows remain separate
when they assert different failure or recovery behavior.

## Cross-repository ownership

Sibling projects have similar concerns around publishing artifacts and
resuming interrupted work, but their current schemas and guarantees differ.
No shared package is proposed from that resemblance alone. Before one is
created, the repositories need an agreed owner and contract for atomicity,
durability, artifact identity, path safety, recovery, and schema migration.
Until then, each repository owns its domain schema and orchestration, with
shared test cases added only after those guarantees are aligned.
