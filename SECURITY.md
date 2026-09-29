# Security policy

## Reporting a vulnerability

Please report vulnerabilities privately through GitHub's
[private vulnerability reporting](https://github.com/NoeFlandre/osm-polygon-description-tag/security/advisories/new),
not in a public issue or pull request.

Include what is affected (CLI command, workflow, published dataset), how to
reproduce it, and the impact you expect. The pipeline handles a Hugging Face
token and publishes a public dataset, so token exposure and anything that could
alter published data are in scope.

## Supported versions

Only the latest code on `main` and the latest published dataset receive fixes.
