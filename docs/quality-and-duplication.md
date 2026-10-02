# Code quality and duplication review

The quality workflow checks each reported function against a strict CRAP score
limit of less than 6. A score of exactly 6 fails. The check has no allowlist.
After the tests, run `just risk` to refresh the full coverage and CRAP reports.

The production and script review merged shared implementations for these items:
numeric coercion, in-memory DuckDB setup, geography count labels, and report
file writing. The risk report construction is now in one cohesive module. It is
separate from its command-line interface. The tests for the CLI, publication,
orchestration, and Grid also use shared domain helpers for repeated inputs.
Each test keeps its own name and assertions.

An AST function-body scan looked at the Python files in `src`, `scripts`, and
`tests`. The scan ignored a leading docstring. It ignored bodies with only
`pass` or an ellipsis. It ignored bodies of less than four nonblank lines. The
scan found one remaining exact production clone: the two lazy package-level
`__getattr__` implementations in `dataset/__init__.py` and
`dataset/languages/__init__.py`. Each one resolves its own export map, imports
on first use, and caches the result. They stay local, so each package keeps its
independent lazy-import boundary. A shared helper would leave two thin wrappers
around the same small body. At that threshold, the scan found no remaining test
function clones across files.

The orchestrator tests share the fake exporter that takes its name from the
source. The timeout propagation test keeps its own fake exporter. It uses a
fixed OSM identifier and keeps its imports inside the callback. These details
are part of the test contract of that fixture. Similar test flows stay separate
when they assert different failure or recovery behavior.

## Cross-repository ownership

Sibling projects have similar concerns about publishing artifacts and resuming
interrupted work. Their current schemas and guarantees are different. This
similarity alone does not justify a shared package. Before the team creates
one, the repositories need an agreed owner and contract for these items:
atomicity, durability, artifact identity, path safety, recovery, and schema
migration. Until then, each repository owns its domain schema and
orchestration. The team adds shared test cases only after the guarantees are
the same.
