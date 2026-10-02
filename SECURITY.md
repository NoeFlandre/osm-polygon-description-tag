# Security policy

## Reporting a vulnerability

Report a vulnerability privately. Use GitHub's
[private vulnerability reporting](https://github.com/NoeFlandre/osm-polygon-description-tag/security/advisories/new).
Do not use a public issue or a pull request.

In the report, state what the vulnerability affects (CLI command, workflow, or
published dataset). Explain how to reproduce it. State the impact that you
expect. The pipeline uses a Hugging Face token and publishes a public dataset.
Token exposure is in scope. Each defect that can change the published data is
also in scope.

## Supported versions

Only the latest code on `main` and the latest published dataset get fixes.
