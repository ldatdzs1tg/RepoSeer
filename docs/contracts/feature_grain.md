# Contract: Feature Grain

Contract `0.1.0`, architecture T-008 v0.3. Canonical feature grain is `(package_id, cutoff_at)` in `PackageSnapshot`; `repo_group_id = repo_id` prevents related packages sharing a repository across data splits. Package↔repository mapping must be verified; repository identity uses the stable GitHub numeric ID.

The horizon is 12 calendar months (`horizon_end_at`), clamping leap-day boundaries. Historical activity is 2019–2025 with quarterly cutoffs 2020–2024. Event/upload time, valid time, known availability and yanked-state observation must be at or before cutoff. Unknown availability is rejected. Current repository observations cannot be backfilled to historical dates.

Each null feature has exactly one `features.<name>` missing-field reason. Zero requires complete observed coverage. Counts across 3/6/12-month windows are nondecreasing. Snapshot feature business computation is not implemented in T-011.

Confirmed ARCHIVED / MAINTENANCE_CEASED / REPLACED-with-cessation events alone support positive labels; negatives require 12-month follow-up, otherwise unknown. Postcutoff labels/evidence are outside feature inputs. Technology/news provide context; dependency impact remains separate from abandonment probability.

See [data contract](data_contract.md) and [generated fields](schema_reference.md#packagesnapshot). Legacy `RepositoryPackageSnapshot` is an internal DTO, not the canonical feature grain.
