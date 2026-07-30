# Test organization

- `unit/runtime`, `unit/osm`, `unit/dataset`, `unit/publication`, and
  `unit/workflow` mirror the canonical source domains.
- `contracts` contains public packaging, CLI, schema, dataset-card, import,
  dependency-direction, and test-safety contracts.
- `integration` contains public CLI/lifecycle and other multi-component
  scenarios.
- `conftest.py` contains only shared fixtures and the suite-wide fail-closed
  Hugging Face subprocess/API guard.

For the final flat-test migration, `test_project_foundation.py` moved to
`contracts` because it specifies the public project metadata and Git exclusion
contract. `test_hermetic_hub_guard.py` moved to `unit/workflow` because its
executable test drives canonical workflow preflight; the shared global guard
remains in `conftest.py`.
