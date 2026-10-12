# Contract: Processed Entities

The canonical public exchange models live in `src/reposeer/contracts/` and use strict Pydantic v2, all keys required, no aliases, explicit null and stable canonical IDs. Supported normalized contract models are:

- `GitHubRepository`: nested `RepoIdentity` and observation/historical provenance.
- `TechnologyEntity`: taxonomy, lifecycle and evidence.
- `Article`: canonical URL/content hashes, publication precision and technology mentions.
- `PackageRelease`: PyPI release, nullable dependency requirements and yanked observation.
- `RepositoryActivity`: stable GitHub activity ID, kind, event time and provenance.

`PackageSnapshot` is the canonical feature-layer contract at `(package_id, cutoff_at)`, not a repository DTO. Collection, normalization, linking and snapshot requests/results are exchange interfaces; only the GitHub repository collector/normalizer has a business implementation in T-011.

`src/reposeer/schemas/` remains legacy/internal DTOs, including `RepositoryRecord`, `CommitRecord`, `ArticleDocument`, `PackageRecord`, `RepositoryPackageSnapshot` and its separate `TechnologyEntity`. These are not public canonical contracts. Legacy DataFrame column validators cannot substitute for `reposeer.contracts.validation`.

`RepositoryService` writes validated repositories to `data/processed/repositories/repositories.parquet` with an explicit flat projection: stable identity, UTC retrieval/valid/availability time, source/raw reference, booleans and nullable counts. Native payload stays only in raw JSONL. DuckDB queries processed Parquet; features use separate Parquet datasets.

Dependency graphs/resolved impact, vulnerabilities/security advisories and abandonment labels are logical T-008 entities without canonical normalized models in T-011. No implementation is claimed for them. See [full contract](data_contract.md) and [generated reference](schema_reference.md).
