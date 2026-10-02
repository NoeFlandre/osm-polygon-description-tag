# Glossary

This page defines the project terms. Each term has one meaning in all the
documentation.

| Term | Meaning |
| --- | --- |
| PBF | An OpenStreetMap data file with the `.osm.pbf` extension. |
| Extract | One PBF file for a region. The tool builds one Parquet file for each extract. |
| Source root | The read-only directory that holds the PBF files. |
| Data root | The directory that holds the generated files, the state, and the logs. |
| Polygon | An area feature from OpenStreetMap that has a `description` tag. |
| GeoParquet | The Parquet file format with geometry columns. The tool writes it for each extract. |
| Geodesic area | The area on the WGS84 ellipsoid. The column name is `area_m2`. |
| Manifest | The JSON file that describes one build output. |
| Dataset card | The `README.md` file that describes the published dataset. |
| Publication | The upload of the allowlisted files to the Hugging Face Hub. |
| Allowlist | The fixed list of files that the tool can upload. |
| Preflight | The check that the tool does before it builds or publishes. |
| Resume | To continue an interrupted run from the saved state. |
| Artifact | A file that the tool generates. |
| CRAP score | The complexity and coverage score of a function. It must be less than 6. |
| Mutant | A small change in the code that the tests must detect. |
| Gate | A check that must pass before you open a pull request. |
