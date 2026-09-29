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

## Acceptance tests

`acceptance/` holds one file per user workflow, each with a module docstring
stating the story and tests named `given_..._when_..._then_...`. Acceptance
means a public CLI or workflow entry point runs on a tiny fixture with only the
network/Hub faked. They carry the `acceptance` marker (`just test-acceptance`).

Decision: plain pytest, not pytest-bdd. There is one maintainer and no
non-developer reader of the specs, and Gherkin step glue would add a layer that
mutation testing and ty cannot check.
