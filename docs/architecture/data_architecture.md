# Data Architecture

## Storage Tiering (T-008 & T-019)

RepoSeer enforces a clear separation of data across storage tiers:

1. **Raw Storage Layer (`data/raw/`)**:
   - Format: JSON Lines (`.jsonl`)
   - Purpose: Store verbatim payloads returned by upstream APIs.
   - Benefits: Preserves provenance, append-only, debuggable, allows replay without API re-fetching.

2. **Processed Storage Layer (`data/processed/`)**:
   - Format: Apache Parquet (`.parquet`)
   - Purpose: Normalized, typed, deduplicated tables aligned with Pydantic contracts.
   - Processing Engine: Polars.
   - Compression: Zstandard / Snappy.

3. **Analytics & Query Layer**:
   - Engine: Embedded DuckDB.
   - Purpose: Direct zero-copy SQL analytics, EDA, feature calculation, and reporting directly over Parquet files.


## Canonical exchange and feature layer (T-008 v0.3 / T-011)

`src/reposeer/contracts/` is the single source of truth for exchange models at schema `0.1.0`; `src/reposeer/schemas/` remains legacy/internal DTOs. Raw strict JSONL preserves native provider payloads; normalized repositories use an explicit flat Parquet projection with provenance; future feature snapshots will use separate Parquet at `(package_id, cutoff_at)`; DuckDB queries Parquet. Repository grouping uses stable GitHub IDs, with verified package↔repository mapping. All exchange timestamps are UTC milliseconds and historical input must be cutoff-safe. See [data contract](../contracts/data_contract.md).

## T-019 physical datasets and handoff

`ProcessedDatasetPipeline(StoragePaths(root)).run(raw_paths, targets=(), cutoff_at=None)`
calls the pure T-018 `NormalizationPipeline.process_jsonl`, then
`ProcessedDatasetWriter.write(result, processing_run_id=...)`. The writer returns
newly inserted row counts; `ProcessingWriteResult` adds the run UUID, status, dataset
states and manifest path. CLI `process` and `scripts/rebuild_processed.py` use this
same implementation. No collector or LLM runs during replay.

Canonical raw paths remain `root/raw/github/{repositories,commits,issues,pull_requests,contributors,releases}.jsonl`
and `root/raw/external/{discovery,documents}.jsonl`. Default processing only discovers
existing files and excludes discovery candidates. Explicit missing/unreadable or
malformed JSONL fails; normalization rejects remain audited. Raw bytes are never
rewritten. All paths, including manifests, resolve from the same `StoragePaths(root)`.

Each table is `root/processed/<dataset>/<dataset>.parquet`, with explicit Polars
types, Zstd compression and deterministic JSON for nested typed values:

| Dataset | Grain / idempotency key | Retained evidence |
|---|---|---|
| repositories | raw_record_id | Existing T-013 flat schema; stable numeric repo identity and nullable counters |
| github_activity | activity_id, raw_record_id | Canonical evidenced event, actual collection run, provenance and missing fields |
| github_activity_details | raw_record_id, entity_kind | Complete typed T-014 entity JSON, native ID and outcome; anonymous ID falls back to raw UUID |
| external_documents | article_id, raw_record_id | Canonical Article fields, content revision, nulls, actual run and provenance |
| external_relevance | article_id, raw_record_id, target_package_id, rule_version | All decisions, exact evidence spans/URLs/link metadata |
| external_lineage | article_id, raw_record_id | Actual source/run IDs and unchanged T-018 lineage metadata |
| processing_audit | processing_run_id, item_type, item_key | Repository/activity/document groups, content duplicates, processing errors, temporal diagnostics and exclusions |

Repository eligibility reuses T-013 policy (private/internal/fork/template/mirror
excluded; archived retained). RepositoryService delegates projection/upsert to the
same writer. The compatible flat repository schema is unchanged; full canonical
state fingerprints and collection run IDs remain in group audit observations.
Repository rows sort by retrieval time, numeric-ID text and raw ID; other tables sort
by their idempotency keys. No observation is replaced by an older snapshot.

Identical keys are skipped, changed values fail, and schema mismatches require
explicit migration. Empty optional inputs preserve old tables and create no empty
schemaless files. The writer also checks canonical state fingerprints across audit
runs; the offline pipeline persists raw observation fingerprints in manifests to
detect changed native payloads even when their projected fields are unchanged.

Processing manifests use
`root/metadata/collection_runs/processing_<processing_run_id>.json`, manifest/dataset
schema version `1`, canonical version `0.1.0`, processing version `0.1.0-t018`.
They contain real input paths, streaming file SHA-256 fingerprints, raw UUID hashes,
actual collection run IDs, inserted/total counts and dataset states, T-018 summary,
sanitized errors and explicit missing reasons. Existing run manifests are not overwritten.
A failed/pending preflight keeps an unknown total row count null, not zero.

Dataset entries additionally expose optional `content_sha256` and `relative_path`,
without changing version `1` or the Parquet schemas. The shared streaming helper
hashes physical file bytes in 1 MiB chunks once per persisted dataset per run.
Written and existing unchanged files receive their actual SHA-256; missing optional
files retain null hashes and zero rows without creating empty Parquet. Hash-read
failure after atomic replacement marks the dataset/run failed, preserves the valid
file and actual inserted/total counts, and propagates the original error for replay.

Portable paths use POSIX `processed/<dataset>/<dataset>.parquet` beneath the storage
root. Downstream consumers should resolve `storage_root / Path(entry.relative_path)`;
the existing `path` remains compatible metadata for the original environment.
Containment is checked before creating dataset directories; traversal, absolute,
drive/UNC and backslash paths are rejected. Legacy manifests missing both optional
fields still load with null values and are not rewritten.

Output hashes describe the physical file at manifest creation time. A later
incremental run can replace the current file; earlier manifests retain their
original hashes and remain readable. Their hash differing from current bytes alone
does not imply corruption. History scans validate raw UUID fingerprints, without
requiring old output hashes to match current files. There is no immutable versioned
Parquet snapshot store.

`processing_status` is complete only for successful persistence with accepted
handoff data and no actual input rejection; partial retains accepted data plus
rejections; failed means no eligible handoff data or an input/persistence failure.
Valid contributor aggregates, GitHub releases and commits without event time remain
accepted internal details, without invented canonical events. Source coverage stays
`not_verified`; zero rows never establish empty upstream history.

Schemas/conflicts are preflighted before writing. Each Parquet uses the existing
atomic temporary-write/replace implementation; the manifest uses `write_json_atomic`.
A later file failure leaves earlier files intact, reports failed/pending datasets
and raises; replay repairs remaining tables without duplicate observation rows.
This is a single-writer design, without multi-file transactions or concurrent writer
locking. Concurrent raw-file changes detected during normalization abort persistence.

DuckDB `register_parquet` validates SQL identifiers and uses a relation API for safe
filesystem path binding, including apostrophes. See README for CLI/query examples.
T-020 can consume `ProcessingWriteResult`, these tables and processing manifests.
It must distinguish audit history from logical entities and retain temporal/coverage
limitations. No D09 features/labels, D10 training, D11 inference or full T-020
collection orchestration is implemented.
